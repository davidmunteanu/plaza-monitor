FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plaza_monitor.py .
COPY seen_listings.json .

# Railway volume will be mounted at /data
# We point SEEN_FILE there so state survives redeploys
ENV SEEN_FILE=/data/seen_listings.json

CMD ["python", "plaza_monitor.py"]
