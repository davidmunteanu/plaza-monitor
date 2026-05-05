# Use the official Playwright image so the browser actually works
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your script
COPY plaza_monitor.py .

# Create a blank JSON file just in case the volume mount is slow
RUN echo "{}" > seen_listings.json

# Tell the script to save data to the Railway persistent volume
ENV SEEN_FILE=/data/seen_listings.json

CMD ["python", "plaza_monitor.py"]