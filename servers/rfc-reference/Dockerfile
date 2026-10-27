FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt mcpo

COPY database.py rfcparse.py fetch.py sync.py main.py ./

ENV RFC_DB=/data/rfc.db
EXPOSE 8000
VOLUME ["/data"]

CMD ["mcpo", "--host", "0.0.0.0", "--port", "8000", "--", "python", "/app/main.py"]
