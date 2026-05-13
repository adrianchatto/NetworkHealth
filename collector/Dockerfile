FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY unifi_collector.py .

# Unbuffered output so logs appear immediately in docker logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "unifi_collector.py"]
