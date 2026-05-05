# Use the official Playwright image so the browser actually works
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your script
COPY plaza_monitor.py .

# Create a blank JSON file right next to the script
RUN echo "{}" > seen_listings.json

# Tell the script to save data locally
ENV SEEN_FILE=seen_listings.json

CMD ["python", "plaza_monitor.py"]