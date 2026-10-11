"""
Sectra Web Banner: Namensgleiche Patient:innen.

POST /banner  Banner-Kontext von Sectra (u.a. PatientId) → Anzahl ANDERER PatientIDs mit
              gleichem Namen + Geburtsdatum bzw. gleichem Namen. Läuft bei jedem Öffnen einer Akte,
              daher nur schnelle (indizierte) PACS-Abfragen.
GET  /banner  ?context=<JSON> → HTML-Gerüst der Detailseite, sofort und ohne PACS-Abfrage.
POST /banner/exact    Fragment: diese Einträge mit aufklappbaren Studien (schnell).
POST /banner/similar  Fragment: Einträge mit ähnlichem Namen + gleichem Geburtsdatum
                      (langsamere Abfragen, unscharfer Vergleich im Dienst); als NDJSON-Stream
                      mit Fortschritt, das Fragment kommt in der letzten Zeile.

Alle Abfragen laufen per DICOMweb (QIDO-RS) auf Studienebene; gezählt wird je distinkter PatientID.
Kein PHI in Logs: weder Request-Body/Query noch PACS-URLs (enthalten Namen) loggen.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import time
import unicodedata
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

# LOG_LEVEL=DEBUG loggt je Anfrage Kontext (inkl. PatientId), Antwort und PACS-Abfragen
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dupes_banner")
logger.setLevel(LOG_LEVEL if LOG_LEVEL in logging.getLevelNamesMapping() else logging.INFO)
# httpx loggt auf INFO die komplette URL inkl. Query (Name) – unterdrücken
logging.getLogger("httpx").setLevel(logging.WARNING)


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


class Settings:
    """ENV-basierte Konfiguration."""

    PACS_BASE_URL: str = _str("PACS_BASE_URL")
    MRN_ISSUER: str = _str("MRN_ISSUER")
    PACS_USERNAME: str = _str("PACS_USERNAME")
    PACS_PASSWORD: str = _str("PACS_PASSWORD")
    # Die erste Abfrage nach einer Pause kann ~55 s dauern
    PACS_TIMEOUT: float = float(_str("PACS_TIMEOUT", "120"))
    PACS_VERIFY_TLS: bool = _str("PACS_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
    # Obergrenze Studienzeilen pro Abfrage; darüber erscheint "+" hinter der Zahl
    PACS_RESULT_LIMIT: int = int(_str("PACS_RESULT_LIMIT", "1000"))

    # Studien nur mit diesen Modalitäten zählen nicht mit (kommagetrennt, leer = aus)
    EXCLUDED_MODALITIES: frozenset[str] = frozenset(
        m.strip().upper() for m in _str("EXCLUDED_MODALITIES", "PX").split(",") if m.strip()
    )
    # Ab dieser Ähnlichkeit (0–1) je Namensteil gilt ein Name auf der Detailseite als ähnlich
    FUZZY_THRESHOLD: float = float(_str("FUZZY_THRESHOLD", "0.8"))
    # Banner-Zahlen je PatientId so lange wiederverwenden (0 = aus)
    BANNER_CACHE_SECONDS: float = float(_str("BANNER_CACHE_SECONDS", "300"))


settings = Settings()

TAG_STUDY_DATE = "00080020"
TAG_STUDY_TIME = "00080030"
TAG_ACCESSION = "00080050"
TAG_MODALITIES = "00080061"
TAG_STUDY_DESCRIPTION = "00081030"
TAG_PATIENT_NAME = "00100010"
TAG_PATIENT_ID = "00100020"
TAG_BIRTH_DATE = "00100030"
TAG_STUDY_UID = "0020000D"

# Liefert das PACS nicht von sich aus, nur per includefield
BANNER_FIELDS = [TAG_MODALITIES]
DETAIL_FIELDS = [TAG_MODALITIES, TAG_STUDY_DESCRIPTION]

# Keine Bildgebung (Präsentationszustände, Befunde, Key Images, Sonstiges, Dokumente)
NON_IMAGING_MODALITIES = frozenset({"PR", "SR", "KO", "OT", "DOC"})

# Das PACS erlaubt keine Suche nur nach Geburtsdatum (HTTP 413), wohl aber Anfangsbuchstabe +
# Geburtsdatum. Alle Initialen parallel sind ~5x schneller als der unindizierte Scan "?*".
SURNAME_INITIALS = "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÜ"

CACHE_MAX_ENTRIES = 2048
_banner_cache: dict[str, tuple[float, dict]] = {}

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# ---------------------------------------------------------------------------
# DICOM-JSON-Helfer
# ---------------------------------------------------------------------------
def _text(value) -> str:
    """Wert als String; PN kommt in DICOM JSON als {"Alphabetic": "Nachname^Vorname"}."""
    if isinstance(value, dict):
        value = value.get("Alphabetic")
    return str(value).strip() if value is not None else ""


def _values(item: dict, tag: str) -> list[str]:
    return [_text(v) for v in (item.get(tag) or {}).get("Value") or []]


def _first(item: dict, tag: str) -> str:
    return next(iter(_values(item, tag)), "")


def _norm_name(name: str) -> str:
    """Vergleichsform: Groß-/Kleinschreibung, Leerzeichen und leere End-Komponenten egal."""
    parts = [p.strip().casefold() for p in name.split("^")]
    while parts and not parts[-1]:
        parts.pop()
    return "^".join(parts)


_TRANSLITERATION = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _fuzzy_key(part: str) -> str:
    """Namensteil für den unscharfen Vergleich: klein, ohne Umlaute/Akzente/Satzzeichen."""
    text = unicodedata.normalize("NFKD", part.casefold().translate(_TRANSLITERATION))
    text = "".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if set(a.split()) <= set(b.split()) or set(b.split()) <= set(a.split()):
        return 0.9  # Doppelname bzw. zusätzlicher Vorname
    return difflib.SequenceMatcher(None, a, b).ratio()


def _fuzzy_reason(name: str, other: str) -> str | None:
    """Warum `other` als ähnlich zu `name` gilt (Text für die Detailseite), sonst None."""
    family, given = (_fuzzy_key(p) for p in (name.split("^") + [""])[:2])
    other_family, other_given = (_fuzzy_key(p) for p in (other.split("^") + [""])[:2])
    threshold = settings.FUZZY_THRESHOLD
    family_sim, given_sim = _similarity(family, other_family), _similarity(given, other_given)
    if family_sim >= threshold and given_sim >= threshold:
        if family_sim == 1.0 and given_sim == 1.0:
            return "andere Schreibweise"
        if family_sim == 1.0:
            return "Vorname ähnlich"
        if given_sim == 1.0:
            return "Nachname ähnlich"
        return "Vor- und Nachname ähnlich"
    if _similarity(family, other_given) >= threshold and _similarity(given, other_family) >= threshold:
        return "Vor- und Nachname vertauscht"
    if given_sim == 1.0:
        return "gleicher Vorname, anderer Nachname"
    return None


def _is_excluded(study: dict) -> bool:
    """True, wenn die Bildgebung der Studie nur aus ausgeschlossenen Modalitäten (z.B. PX) besteht."""
    imaging = {m.upper() for m in _values(study, TAG_MODALITIES)} - NON_IMAGING_MODALITIES
    return bool(imaging) and imaging <= settings.EXCLUDED_MODALITIES


def _group_by_patient(studies: list[dict], keep) -> list[dict]:
    """
    Studienzeilen nach distinkten PatientIDs zusammenfassen; keep(pid, study) filtert.
    Ausgeschlossene Studien entfallen – PatientIDs, die nur solche haben, damit ebenfalls.
    """
    patients: dict[str, dict] = {}
    for study in studies:
        pid = _first(study, TAG_PATIENT_ID)
        if not pid or _is_excluded(study) or not keep(pid, study):
            continue
        patient = patients.setdefault(
            pid,
            {
                "id": pid,
                "name": _first(study, TAG_PATIENT_NAME),
                "dob": _first(study, TAG_BIRTH_DATE),
                "studies": {},
            },
        )
        uid = _first(study, TAG_STUDY_UID) or str(len(patient["studies"]))
        patient["studies"][uid] = {
            "date": _first(study, TAG_STUDY_DATE),
            "time": _first(study, TAG_STUDY_TIME),
            "accession": _first(study, TAG_ACCESSION),
            "modalities": ", ".join(_values(study, TAG_MODALITIES)),
            "description": _first(study, TAG_STUDY_DESCRIPTION),
        }
    for patient in patients.values():
        patient["studies"] = sorted(
            patient["studies"].values(), key=lambda s: (s["date"], s["time"]), reverse=True
        )
    return sorted(patients.values(), key=lambda p: (p["name"].casefold(), p["dob"], p["id"]))


# ---------------------------------------------------------------------------
# PACS-Abfragen
# ---------------------------------------------------------------------------
def make_client() -> httpx.AsyncClient:
    if not settings.PACS_USERNAME or not settings.PACS_PASSWORD:
        raise RuntimeError("PACS_USERNAME and PACS_PASSWORD must be set in environment")
    return httpx.AsyncClient(
        auth=(settings.PACS_USERNAME, settings.PACS_PASSWORD),
        verify=settings.PACS_VERIFY_TLS,
        timeout=settings.PACS_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
        # Begrenzt gleichzeitig die Last auf dem PACS
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def query_studies(
    client: httpx.AsyncClient, params: dict, limit: int | None = None
) -> tuple[list[dict], bool]:
    """QIDO-RS Studiensuche. Liefert (Studien, truncated)."""
    base = settings.PACS_BASE_URL.rstrip("/")
    url = f"{base}/SectraQidoRs/configs/dicomHeaders/mrnissuers/{settings.MRN_ISSUER}/studies"
    params = {**params, "limit": limit or settings.PACS_RESULT_LIMIT}
    started = time.monotonic()
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.debug("pacs query %s -> HTTP %s: %.300s", params, e.response.status_code, e.response.text)
        raise RuntimeError(f"PACS study query failed: HTTP {e.response.status_code}") from None
    except httpx.RequestError as e:
        raise RuntimeError(f"PACS study query failed: {e.__class__.__name__}") from None
    # Sectra: 'Warning: 299 - "There are N additional results that can be requested"'
    truncated = "299" in response.headers.get("warning", "")
    studies = response.json() if response.content.strip() else []
    logger.debug(
        "pacs query %s -> HTTP %s, %d rows%s in %.2fs",
        params, response.status_code, len(studies), " (truncated)" if truncated else "",
        time.monotonic() - started,
    )
    return studies, truncated


async def _lookup_patient(client: httpx.AsyncClient, patient_id: str) -> tuple[str, str] | None:
    """(Name, Geburtsdatum) zur PatientId; None, wenn es keine Studie / keinen Namen gibt."""
    own, _ = await query_studies(client, {TAG_PATIENT_ID: patient_id}, limit=1)
    name = _first(own[0], TAG_PATIENT_NAME) if own else ""
    return (name, _first(own[0], TAG_BIRTH_DATE)) if name else None


async def find_namesakes(client: httpx.AsyncClient, patient_id: str, details: bool) -> dict | None:
    """
    Sucht ANDERE PatientIDs mit gleichem Namen (same_name; same_dob markiert gleiches
    Geburtsdatum). Mit details=True zusätzlich Studienfelder für die Anzeige.
    None, wenn es zur PatientId keine Studie / keinen Namen gibt.
    """
    own = await _lookup_patient(client, patient_id)
    if own is None:
        return None
    name, dob = own
    target = _norm_name(name)
    extra = {"includefield": DETAIL_FIELDS if details else BANNER_FIELDS}

    # Das PACS vergleicht PN wörtlich inkl. End-Carets ("A^B" findet "A^B^^^" nicht),
    # daher mit Wildcard suchen und lokal auf Namensgleichheit filtern.
    pattern = name.rstrip("^ ") + "*"
    name_rows, name_truncated = await query_studies(client, {TAG_PATIENT_NAME: pattern, **extra})

    dob_truncated = False
    if name_truncated and dob:
        # Nur bei sehr häufigen Namen nötig: die enge Abfrage hält die wichtigere Zahl exakt
        dob_rows, dob_truncated = await query_studies(
            client, {TAG_PATIENT_NAME: pattern, TAG_BIRTH_DATE: dob, **extra}
        )
        name_rows = name_rows + dob_rows

    def is_namesake(pid: str, study: dict) -> bool:
        # Wildcard-Treffer wie "A^Berta" für "A^Bert" aussortieren
        return pid != patient_id and _norm_name(_first(study, TAG_PATIENT_NAME)) == target

    same_name = _group_by_patient(name_rows, is_namesake)
    for patient in same_name:
        patient["same_dob"] = bool(dob) and patient["dob"] == dob

    return {
        "patient": {"id": patient_id, "name": name, "dob": dob},
        "same_name": same_name,
        "same_name_truncated": name_truncated,
        "same_dob_truncated": dob_truncated,
    }


async def find_similar(client: httpx.AsyncClient, patient_id: str, on_progress=None) -> dict | None:
    """
    Sucht ANDERE PatientIDs mit ähnlichem Namen und gleichem Geburtsdatum (similar, mit
    "reason"); similar ist None, wenn das Geburtsdatum fehlt. Dauert Sekunden – nur Detailseite.
    None, wenn es zur PatientId keine Studie / keinen Namen gibt.
    on_progress(done, total) meldet jede abgeschlossene Initialen-Abfrage.
    """
    own = await _lookup_patient(client, patient_id)
    if own is None:
        return None
    name, dob = own
    if not dob:
        return {"similar": None, "similar_truncated": False}
    target = _norm_name(name)

    # Alle am selben Tag Geborenen holen und im Dienst unscharf vergleichen.
    # Eigene Initialen ergänzen (z.B. "Ç", "Ø").
    own_initials = {part.strip()[:1].upper() for part in name.split("^")[:2]} - {"", "*", "?"}
    initials = sorted(set(SURNAME_INITIALS) | own_initials)
    done = 0

    def report() -> None:
        if on_progress:
            on_progress(done, len(initials))

    async def query_initial(initial: str) -> tuple[list[dict], bool]:
        nonlocal done
        result = await query_studies(
            client,
            {TAG_PATIENT_NAME: f"{initial}*", TAG_BIRTH_DATE: dob, "includefield": DETAIL_FIELDS},
        )
        done += 1
        report()
        return result

    report()
    results = await asyncio.gather(*[query_initial(initial) for initial in initials])

    def is_similar(pid: str, study: dict) -> bool:
        other = _first(study, TAG_PATIENT_NAME)
        return (
            pid != patient_id
            and _norm_name(other) != target
            and _first(study, TAG_BIRTH_DATE) == dob
            and _fuzzy_reason(name, other) is not None
        )

    similar = _group_by_patient([row for rows, _ in results for row in rows], is_similar)
    for patient in similar:
        patient["reason"] = _fuzzy_reason(name, patient["name"])
    return {"similar": similar, "similar_truncated": any(truncated for _, truncated in results)}


async def banner_counts(client: httpx.AsyncClient, patient_id: str) -> dict | None:
    """Zahlen fürs Banner, kurz gecacht (Sectra fragt bei jedem Öffnen der Akte an)."""
    now = time.monotonic()
    cached = _banner_cache.get(patient_id)
    if cached and cached[0] > now:
        logger.debug("banner cache hit for %s", patient_id)
        return cached[1]

    result = await find_namesakes(client, patient_id, details=False)
    counts = None
    if result:
        has_dob = bool(result["patient"]["dob"])
        counts = {
            "same_name": len(result["same_name"]),
            "same_name_truncated": result["same_name_truncated"],
            "same_dob": sum(p["same_dob"] for p in result["same_name"]) if has_dob else None,
            "same_dob_truncated": result["same_dob_truncated"],
        }
    if settings.BANNER_CACHE_SECONDS > 0:
        if len(_banner_cache) >= CACHE_MAX_ENTRIES:
            _banner_cache.clear()
        _banner_cache[patient_id] = (now + settings.BANNER_CACHE_SECONDS, counts)
    return counts


# ---------------------------------------------------------------------------
# Endpunkte
# ---------------------------------------------------------------------------
async def health(request: Request):
    return JSONResponse({"status": "ok"})


def _banner_response(context: dict, text: str, level: int) -> JSONResponse:
    logger.debug("banner -> 200 %r level %d", text, level)
    return JSONResponse(
        {
            "ApplicableForContext": context,
            "TextContent": text,
            "TextContent2": "",
            "WebBannerInfoLevel": level,
            "BigWindowHeight": 533,
            "BigWindowWidth": 1000,
        }
    )


async def banner(request: Request):
    try:
        context = await request.json()
    except Exception:
        context = None
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "banner <- content-type %r, context %r, raw body %.500r",
            request.headers.get("content-type"), context, await request.body(),
        )
    if not isinstance(context, dict):
        logger.debug("banner -> 400 no context")
        return JSONResponse({"error": "No context provided"}, status_code=400)
    patient_id = str(context.get("PatientId") or "").strip()
    if not patient_id:
        # kein Fall geöffnet: neutrales Banner statt "Error" in der IDS7
        return _banner_response(context, "Dupes", 0)

    try:
        counts = await banner_counts(request.app.state.client, patient_id)
    except Exception as e:
        logger.error("banner failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)

    def fmt(n: int | None, truncated: bool) -> str:
        return "?" if n is None else f"{n}{'+' if truncated else ''}"

    if counts is None:
        text, level = "D: ?", 0
    else:
        if counts["same_dob"]:
            # Name + Geburtsdatum gleich: nur diese Zahl
            text = f"D: {fmt(counts['same_dob'], counts['same_dob_truncated'])}"
        elif counts["same_name"]:
            # nur Namensgleiche: "0 / X" ("? / X", wenn das Geburtsdatum fehlt)
            text = (
                f"D: {fmt(counts['same_dob'], False)} / "
                f"{fmt(counts['same_name'], counts['same_name_truncated'])}"
            )
        else:
            text = "D: Keine"
        # 1 = grün (keine), 2 = orange (Namensgleiche), 3 = rot (Name + Geburtsdatum gleich)
        level = 3 if counts["same_dob"] else 2 if counts["same_name"] else 1
    return _banner_response(context, text, level)


async def details(request: Request):
    """
    Detailseite, die Sectra beim Klick auf das Banner öffnet (?context=<JSON>).
    Liefert sofort nur das Gerüst; die Ergebnisse lädt die Seite per POST /banner/{part} nach.
    """
    try:
        context = json.loads(request.query_params.get("context") or "")
    except ValueError:
        context = None
    patient_id = str(context.get("PatientId") or "").strip() if isinstance(context, dict) else ""
    logger.debug("details <- query %r -> PatientId %r", request.url.query, patient_id)
    return templates.TemplateResponse(
        request,
        "details.html",
        {"patient_id": patient_id},
        status_code=200 if patient_id else 400,
        headers={"Cache-Control": "no-store"},
    )


DETAIL_PARTS = {
    # Teil → (Suchfunktion, Template); "exact" ist schnell, "similar" dauert Sekunden
    "exact": (lambda client, pid: find_namesakes(client, pid, details=True), "exact.html"),
    "similar": (find_similar, "similar.html"),
}


async def details_part(request: Request):
    """HTML-Fragment für einen Teil der Detailseite. POST, damit die PatientId in keiner URL steht."""
    if request.path_params["part"] not in DETAIL_PARTS:
        return JSONResponse({"error": "Not found"}, status_code=404)
    find, template = DETAIL_PARTS[request.path_params["part"]]
    try:
        body = await request.json()
    except Exception:
        body = None
    patient_id = str(body.get("PatientId") or "").strip() if isinstance(body, dict) else ""

    page = {"error": None, "result": None}
    status = 200
    if not patient_id:
        page["error"], status = "Keine PatientId übergeben.", 400
    elif find is find_similar:
        return _similar_stream(request.app.state.client, patient_id)
    else:
        try:
            page["result"] = await find(request.app.state.client, patient_id)
        except Exception as e:
            logger.error("details %s failed: %s", request.path_params["part"], e)
            page["error"], status = f"PACS-Abfrage fehlgeschlagen: {e}", 502
    return templates.TemplateResponse(
        request,
        template,
        page,
        status_code=status,
        # X-Fragment: daran erkennt die Seite echte Antworten (statt z.B. Fehlerseiten des Proxys)
        headers={"Cache-Control": "no-store", "X-Fragment": "1"},
    )


def _similar_stream(client: httpx.AsyncClient, patient_id: str) -> StreamingResponse:
    """
    Suche nach ähnlichen Namen als NDJSON-Stream: je abgeschlossener PACS-Abfrage eine Zeile
    {"done": n, "total": m}, zum Schluss {"html": <Fragment>}. Der Status steht schon beim
    ersten Byte fest, daher immer 200 – ein PACS-Fehler steht als Text im Fragment.
    """

    async def lines():
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def search() -> None:
            page = {"error": None, "result": None}
            try:
                page["result"] = await find_similar(
                    client, patient_id, lambda done, total: queue.put_nowait({"done": done, "total": total})
                )
            except Exception as e:
                logger.error("details similar failed: %s", e)
                page["error"] = f"PACS-Abfrage fehlgeschlagen: {e}"
            queue.put_nowait({"html": templates.get_template("similar.html").render(page)})

        task = asyncio.create_task(search())
        try:
            while True:
                item = await queue.get()
                yield json.dumps(item) + "\n"
                if "html" in item:
                    break
        finally:
            # Fenster geschlossen: laufende PACS-Abfragen abbrechen
            task.cancel()

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        # X-Accel-Buffering: nginx soll den Stream nicht puffern
        headers={"Cache-Control": "no-store", "X-Fragment": "1", "X-Accel-Buffering": "no"},
    )


def _format_date(value: str) -> str:
    """DICOM DA (YYYYMMDD) → DD.MM.YYYY."""
    return f"{value[6:8]}.{value[4:6]}.{value[0:4]}" if len(value) == 8 and value.isdigit() else value


def _format_time(value: str) -> str:
    """DICOM TM (HHMMSS.ffffff) → HH:MM."""
    return f"{value[0:2]}:{value[2:4]}" if len(value) >= 4 and value[:4].isdigit() else value


def _format_name(value: str) -> str:
    """DICOM PN (Nachname^Vorname^…) → "Nachname, Vorname"."""
    return ", ".join(p.strip() for p in value.split("^")[:2] if p.strip())


templates.env.filters.update(dicom_date=_format_date, dicom_time=_format_time, dicom_name=_format_name)


@asynccontextmanager
async def _lifespan(app: Starlette):
    app.state.client = make_client()
    yield
    await app.state.client.aclose()


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/banner", banner, methods=["POST"]),
        Route("/banner", details, methods=["GET"]),
        Route("/banner/{part:str}", details_part, methods=["POST"]),
    ],
    lifespan=_lifespan,
)
