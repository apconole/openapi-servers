FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt mcpo

COPY database.py sync.py main.py config.yaml.example ./

ENV MLIST_DB=/data/lists.db
EXPOSE 8000
VOLUME ["/data"]

CMD ["mcpo", "--host", "0.0.0.0", "--port", "8000", "--", "python", "/app/main.py"]
