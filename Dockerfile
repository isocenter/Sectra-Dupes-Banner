FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# pip wird zur Laufzeit nicht gebraucht – entfernen spart ~15 MB
RUN pip install --no-cache-dir --no-compile -r requirements.txt \
    && pip uninstall -y pip

COPY app.py .
COPY templates/ templates/

USER nobody
EXPOSE 8000
# --no-access-log: die Query der Detailseite enthält die PatientId
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
