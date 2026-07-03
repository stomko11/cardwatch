FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/config

VOLUME /app/config
VOLUME /app/data

EXPOSE 8099

ENV CONFIG_PATH=/app/config/config.yaml

LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/stomko11/cardwatch/main/icon.png"
LABEL net.unraid.docker.webui="http://[IP]:[PORT:8099]/"

CMD ["python", "-m", "oscam_monitor.main"]
