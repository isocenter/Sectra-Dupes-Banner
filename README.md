# Sectra Web Banner: Namensgleiche Patient:innen („Dubletten“)

Kleiner Web-Dienst für ein Sectra-PACS-Web-Banner. Er zeigt bei jedem Öffnen eines Falls, ob es im PACS **andere PatientIDs** mit gleichem Namen (und Geburtsdatum) gibt – ein Hinweis auf doppelt angelegte Patient:innen oder Verwechslungsgefahr. Ein Klick auf das Banner öffnet eine Detailseite mit diesen Einträgen und ihren Studien.

Als Datenquelle dient ausschließlich das PACS per DICOMweb (QIDO-RS).

## Banner-Anzeige

Der Text steht in `TextContent`, `TextContent2` bleibt leer. Gezählt werden **andere** PatientIDs;
die aktuell geöffnete PatientID zählt nie mit.

| Anzeige | Bedeutung | `WebBannerInfoLevel` |
|---------|-----------|----------------------|
| `D: Keine` | keine Namensgleichen | 1 (grün) |
| `D: 2` | 2 andere PatientIDs mit gleichem Namen **und** Geburtsdatum. Weitere Namensgleiche mit anderem Geburtsdatum erscheinen dann nur auf der Detailseite. | 3 (rot) |
| `D: 0 / 3` | kein Treffer mit gleichem Geburtsdatum, aber 3 Namensgleiche | 2 (orange) |
| `D: ? / 3` | wie oben, aber zur aktuellen PatientID fehlt das Geburtsdatum | 2 (orange) |
| `D: ?` | zur aktuellen PatientID gibt es im PACS keine Studie bzw. keinen Namen | 0 |
| `Dupes` | der Kontext enthält keine `PatientId` (kein Fall geöffnet) | 0 |
| `…+` | ein `+` hinter einer Zahl: das PACS hat die Trefferliste gekürzt, es gibt mindestens so viele | |

## Endpunkte

| Methode | Pfad | Zweck |
|---------|------|-------|
| `POST` | `/banner` | Banner-Kontext von Sectra (JSON) → Banner-Antwort (JSON). Wird bei jedem Öffnen eines Falls aufgerufen. |
| `GET` | `/banner?context=<JSON>` | Detailseite (HTML), die die IDS7 beim Klick auf das Banner öffnet. |
| `POST` | `/banner/exact` | HTML-Fragment mit den Namensgleichen; wird nachgeladen. |
| `POST` | `/banner/similar` | HTML-Fragment mit den ähnlichen Namen (dauert Sekunden); wird nachgeladen. |
| `GET` | `/health` | `{"status":"ok"}` – für den Docker-Healthcheck. |

| Status | Fall |
|--------|------|
| 200 | Normalfall, auch `D: ?` und `Dupes` (`PatientId` fehlt) |
| 400 | kein JSON-Objekt |
| 502 | PACS nicht erreichbar oder Fehlerantwort des PACS, z.B. `{"error": "PACS study query failed: HTTP 401"}` |

### `GET /banner?context=<JSON>`

Die Seite zeigt drei Gruppen:

1. **Gleicher Name und gleiches Geburtsdatum** (rot)
2. **Gleicher Name, anderes Geburtsdatum** (orange)
3. **Ähnlicher Name und gleiches Geburtsdatum** (blau) – unscharfer Vergleich, siehe unten. Jede Zeile trägt ein Etikett mit dem Grund, z.B. „Nachname ähnlich“. Diese Suche dauert Sekunden und läuft deshalb nur hier, nicht im Banner: das Banner kann `D: Keine` zeigen, obwohl es hier Treffer gibt.

Jede Zeile (Name, Geburtsdatum, ID, Anzahl Studien) lässt sich aufklappen und zeigt dann die Studien: Datum, Zeit, Accession, Modalität, Beschreibung. Das Aufklappen kostet keine weitere PACS-Abfrage. Alle Antworten tragen `Cache-Control: no-store`.

| Status | Gerüst (`GET /banner`) | Fragmente (`POST /banner/…`) |
|--------|------------------------|------------------------------|
| 200 | `PatientId` im Kontext vorhanden | Normalfall; auch wenn es zur PatientId keine Studien gibt (Hinweis im Fragment) |
| 400 | `context` fehlt, ist kein JSON oder enthält keine `PatientId` | `PatientId` fehlt im Body |
| 404 | – | unbekannter Teil |
| 502 | – | PACS-Abfrage fehlgeschlagen (Fehlertext im Fragment) |

## Funktionsweise

Alle Abfragen gehen an
`{PACS_BASE_URL}/SectraQidoRs/configs/dicomHeaders/mrnissuers/{MRN_ISSUER}/studies`:

| # | Abfrage | Wann | Dauer |
|---|---------|------|-------|
| 1 | `00100020=<PatientId>&limit=1` → Name, Geburtsdatum | immer | ms |
| 2 | `00100010=<Name>*&includefield=00080061` → Namensgleiche | immer | ms |
| 3 | `00100010=<Name>*&00100030=<GebDat>` | nur wenn Abfrage 2 gekürzt wurde | ms |
| 4 | `00100010=<Initiale>*&00100030=<GebDat>` für A–Z, Ä, Ö, Ü (29 Abfragen parallel) | nur Detailseite | zusammen 2–3 s |

Die Detailseite fordert zusätzlich `includefield=00081030` (Studienbeschreibung) an.

### Regeln beim Zählen

- **Gezählt wird je distinkter PatientID.** Das PACS bietet nur eine Suche auf Studienebene; eine Person mit 200 Studien kommt als 200 Zeilen zurück.
- **Namensvergleich:** Groß-/Kleinschreibung, Leerzeichen am Rand und leere End-Komponenten werden ignoriert (`Muster^Anna` = `MUSTER^Anna^^^`). Alle belegten Komponenten müssen übereinstimmen.  Gesucht wird mit `<Name>*`, danach wird lokal verglichen – so fällt `Muster^Annabell` wieder heraus.
- **Geburtsdatum** wird lokal aus den Treffern der Namenssuche verglichen. Die enge Abfrage 3 läuft nur, wenn die Namenssuche gekürzt wurde, damit die wichtigere Zahl auch bei sehr häufigen Namen stimmt.
- **Ausgeschlossene Modalitäten:** Studien, deren Bildgebung nur aus `EXCLUDED_MODALITIES` besteht (Default `PX`, Panorama Zahn Film), werden ignoriert – im Banner und auf der Detailseite. `PR`, `SR`, `KO`, `OT`, `DOC` gelten nicht als Bildgebung: `PX,PR` wird ignoriert, `PX,CT` nicht. PatientIDs, die nur solche Studien haben, zählen nicht mit. QIDO kennt kein „ungleich“, daher wird die Modalität mitgeliefert (`includefield=00080061`) und lokal gefiltert.

### Unscharfer Namensvergleich (Detailseite)

Das PACS kann weder unscharf suchen noch nur nach Geburtsdatum. Abfrage 4 holt deshalb alle am selben Tag Geborenen über den Anfangsbuchstaben des Nachnamens (zusätzlich die Initialen des aktuellen Namens, falls sie nicht in A–Z/ÄÖÜ liegen); verglichen wird im Dienst (`_fuzzy_reason`):

- **Normalisierung** je Namensteil: Kleinschreibung, `ä→ae`, `ö→oe`, `ü→ue`, `ß→ss`, Akzente entfernt, Bindestriche und Satzzeichen wie Leerzeichen.
- **Ähnlichkeit** je Namensteil (Nachname, Vorname): 1,0 bei Gleichheit; 0,9 wenn die Wörter des einen im anderen enthalten sind (Doppelname, zweiter Vorname); sonst `difflib.SequenceMatcher` (0–1).
- **Treffer**, wenn das Geburtsdatum gleich ist, der Name nicht ohnehin als „gleich“ gilt und einer dieser Fälle zutrifft (Schwelle `FUZZY_THRESHOLD`, Default 0,8):

| Etikett | Bedingung | Beispiel |
|---------|-----------|----------|
| andere Schreibweise | beide Teile nach Normalisierung gleich | `Müller-Lang^Jörg` / `Mueller Lang^Joerg` |
| Nachname ähnlich | Vorname gleich, Nachname ≥ Schwelle | `Meier` / `Meyer`, `Muster-Beispiel` / `Muster` |
| Vorname ähnlich | Nachname gleich, Vorname ≥ Schwelle | `Anna` / `Ana`, `Anna Maria` / `Anna` |
| Vor- und Nachname ähnlich | beide ≥ Schwelle | `Mustre^Ana` / `Muster^Anna` |
| Vor- und Nachname vertauscht | über Kreuz ≥ Schwelle | `Anna^Muster` / `Muster^Anna` |
| gleicher Vorname, anderer Nachname | Vorname gleich, Nachname beliebig | Namensänderung |

Bewusst kein Treffer: gleicher Nachname mit deutlich anderem Vornamen (`Anna` / `Lena`, auch `Anna` / `Anne` liegt mit 0,75 unter der Schwelle) – das sind z.B. Zwillinge.

### Ressourcenschonung

- Ein gemeinsamer HTTP-Client mit Connection-Pool, höchstens 10 gleichzeitige Verbindungen zum PACS.
- Normalfall: 2 schnelle Abfragen pro Banner.
- Jede Abfrage ist auf `PACS_RESULT_LIMIT` Studienzeilen begrenzt.
- Die Banner-Zahlen werden je PatientId `BANNER_CACHE_SECONDS` (Default 5 min) im Speicher gehalten; wiederholtes Öffnen desselben Falls kostet keine PACS-Abfrage. Die Detailseite fragt immer frisch.

## Was das PACS kann – und was nicht

Ergebnisse von Tests gegen das produktive PACS (Sectra QIDO-RS, Stand Oktober 2026):

| Thema | Befund |
|-------|--------|
| Namenssuche | wörtlich inkl. End-Carets (`A^B` findet `A^B^^^` **nicht**), aber unabhängig von Groß-/Kleinschreibung |
| Wildcards | `*` und `?` funktionieren, auch mitten im Namen (`Mu*er^A*`, `M?ster^Anna*`) |
| Führendes `*` | abgelehnt: HTTP 413 „Query is too wide. Include at least one indexed search key without leading wildcard.“ |
| Nur Geburtsdatum | ebenfalls HTTP 413 |
| `?*` + Geburtsdatum | erlaubt (umgeht die Prüfung auf führendes `*`), läuft aber ohne Index: alle am Tag Geborenen in 10–13 s; `?*^Vorname*` in 1–4 s, ohne Geburtsdatum ~20 s |
| Initiale + Geburtsdatum | `R*` + Geburtsdatum ist erlaubt; alle 29 Initialen parallel liefern dieselben Treffer wie `?*` in 2–3 s |
| Unscharfe Suche | `fuzzymatching=true` wird nicht unterstützt (`Warning: 299 … Only literal matching has been performed.`) |
| `limit` | funktioniert; gekürzte Listen meldet das PACS mit `Warning: 299 - "There are N additional results that can be requested"` |
| `includefield` | funktioniert einzeln, mehrfach und als `includefield=all` |
| Kein Treffer | HTTP 204 ohne Body |
| Kaltstart | die erste Abfrage nach einer Pause dauerte ~55 s, alle weiteren < 1 s |

Felder pro Studie in der schnellen Namenssuche:

| Standardmäßig geliefert | Nur per `includefield` |
|-------------------------|------------------------|
| PatientName `00100010`, PatientID `00100020`, IssuerOfPatientID `00100021`, Geburtsdatum `00100030`, Geschlecht `00100040`, OtherPatientIDsSequence `00101002` | ModalitiesInStudy `00080061`, StudyDescription `00081030` |
| StudyDate `00080020`, StudyTime `00080030`, AccessionNumber `00080050`, ReferringPhysicianName `00080090` | InstitutionName `00080080`, PerformingPhysicianName `00081050`, BodyPartExamined `00180015` |
| StudyInstanceUID `0020000D`, StudyID `00200010`, RetrieveURL `00081190` | NumberOfStudyRelatedSeries `00201206`, …Instances `00201208`, PPS-Beschreibung `00400254` |

## Konfiguration

Über Umgebungsvariablen, in Docker aus `.env` (Vorlage: `.env.example`).

| Variable | Pflicht | Default | Bedeutung |
|----------|---------|---------|-----------|
| `PACS_USERNAME` | ja | – | Konto für die Server-zu-Server-Anmeldung am PACS (Basic Auth) |
| `PACS_PASSWORD` | ja | – | Passwort dazu, siehe Hinweis unten |
| `PACS_BASE_URL` | nein | | Basis-URL des PACS |
| `MRN_ISSUER` | nein | | MRN-Issuer im QIDO-Pfad |
| `PACS_TIMEOUT` | nein | `120` | Timeout je PACS-Abfrage in Sekunden (wegen Kaltstart ~55 s nicht zu knapp wählen) |
| `PACS_VERIFY_TLS` | nein | `false` | TLS-Zertifikat des PACS prüfen (`true`/`false`) |
| `PACS_RESULT_LIMIT` | nein | `1000` | maximale Studienzeilen je Abfrage |
| `EXCLUDED_MODALITIES` | nein | `PX` | kommagetrennte Modalitäten, die nicht zählen; leer = aus |
| `FUZZY_THRESHOLD` | nein | `0.8` | Mindest-Ähnlichkeit (0–1) je Namensteil für „ähnlicher Name“ auf der Detailseite |
| `BANNER_CACHE_SECONDS` | nein | `300` | Haltezeit der Banner-Zahlen je PatientId; `0` = aus |
| `LOG_LEVEL` | nein | `INFO` | `DEBUG` loggt je Anfrage den Sectra-Kontext (inkl. PatientId und Suchparametern wie Name), die Banner-Antwort und jede PACS-Abfrage mit Dauer und Zeilenzahl – nur zur Fehlersuche einschalten |

## Deployment

### Container

```bash
cp .env.example .env          # PACS_USERNAME / PACS_PASSWORD eintragen
docker compose up -d --build
docker compose ps             # Status sollte "healthy" werden
```

Der Dienst lauscht nur lokal auf `127.0.0.1:43002` (im Container Port 8000) und startet nach einem Reboot von selbst (`restart: unless-stopped`).

Besonderheiten in `docker-compose.yml`:

- **Proxy-Variablen werden geleert.** Der Docker-Host injiziert `HTTP_PROXY`/`HTTPS_PROXY` in jeden Container; ohne das Leeren läuft der Healthcheck über den Proxy und trifft einen fremden Dienst. (Der PACS-Client ignoriert Proxy-Variablen ohnehin, `trust_env=False`.)
- **Härtung:** läuft als `nobody`, Dateisystem schreibgeschützt, keine Capabilities, `no-new-privileges`, 128 MB Speichergrenze (Verbrauch ≈ 25 MB).
- **Access-Log aus** (`--no-access-log` im `Dockerfile`), siehe Datenschutz.

## Datenschutz und Sicherheit

- **Keine personenbezogenen Daten in Logs.** Der Dienst loggt weder Request-Body noch PACS-URLs (die Query enthält den Namen); httpx-Logging ist dafür auf WARNING gesetzt. Das Access-Log ist aus, weil die Query der Detailseite die PatientId enthält. Im Reverse-Proxy ebenfalls ohne Query-String loggen.
- **Keine eigene Authentifizierung.** Die Detailseite zeigt Namen, Geburtsdaten und Studien zu jeder übergebenen PatientId. Der Dienst darf nur intern erreichbar sein; Zugriffsbeschränkung gehört in den Reverse-Proxy. Von Sectra mitgeschickte Header (z.B. `Authorization`, `X-Sectra-ImpersonateSession`) werden nicht geprüft.
- **Keine Persistenz.** Im Speicher liegen nur PatientId → Zahlen (Cache); nichts wird auf Platte geschrieben.
- **TLS-Prüfung zum PACS ist standardmäßig aus** (`PACS_VERIFY_TLS=false`).  Mit passender CA im Image auf `true` stellen.
- **Geheimnisse** stehen nur in `.env`; die Datei ist in `.gitignore` und `.dockerignore` ausgeschlossen und landet weder im Repository noch im Image.
