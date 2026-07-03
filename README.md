# 📡 OScam Monitor

[![Docker Hub](https://img.shields.io/docker/pulls/stomko11/oscam-monitor?label=Docker%20Hub)](https://hub.docker.com/r/stomko11/oscam-monitor)
[![GitHub Container Registry](https://img.shields.io/badge/ghcr.io-stomko11%2Foscam--monitor-blue)](https://ghcr.io/stomko11/oscam-monitor)
[![GitHub release](https://img.shields.io/github/v/tag/stomko11/oscam-monitor?label=version)](https://github.com/stomko11/oscam-monitor/releases)

A self-hosted monitoring dashboard for OScam servers. Connects to OScam WebIF API to track active clients, build channel mappings, collect viewing statistics, and manage reader/card status — all from a single Docker container.

## Features

- **Live view** — who's watching what right now, with ECM times and reader info
- **Channel mapping** — auto-discovers CAID:SID → channel name from OScam logs and Enigma2 receivers
- **Viewing statistics** — per-user watch time, most popular channels, daily activity charts
- **Reader management** — reader status, entitlements, card serial tracking with Pushover alerts
- **File sync** — manage oscam.srvid2 and oscam.services directly from the UI, with diff suggestions and backups
- **Auto-mapping** — links OScam users to VU+/Enigma2 receivers for automatic channel name resolution
- **SK/CZ tagging** — automatically detects Slovak vs Czech channels based on reader/card decoding patterns

## Quick Start

### Unraid

1. In Unraid, go to **Docker → Template Repositories**
2. Add: `https://github.com/stomko11/oscam-monitor`
3. Click **Add Container**, select the **OScam Monitor** template
4. Configure your OScam server details in the config file
5. Click **Apply** — done!

Data is stored in `/mnt/user/appdata/oscam-monitor` by default.

### Docker Compose

```yaml
services:
  oscam-monitor:
    image: stomko11/oscam-monitor:latest
    container_name: oscam-monitor
    ports:
      - "8099:8099"
    volumes:
      - ./config:/app/config
      - ./data:/app/data
    environment:
      - CONFIG_PATH=/app/config/config.yaml
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open http://localhost:8099

### Alternative registries

- Docker Hub: `stomko11/oscam-monitor:latest`
- GitHub Container Registry: `ghcr.io/stomko11/oscam-monitor:latest`

## Configuration

Copy `config/config.example.yaml` to `config/config.yaml` and edit:

```yaml
server:
  name: My-OScam
  host: 192.168.1.100
  port: 8888
  username: admin
  password: yourpassword
  log_source: api

user_device_map:
  oscam-username:
    ip: 192.168.1.50
    port: 80
    username: root
    password: receiverpass

web:
  host: 0.0.0.0
  port: 8099
  timezone: Europe/Bratislava
  username: admin
  password: admin

stats:
  session_timeout_seconds: 60
  retention_days: 90
```

Most settings can also be configured via the **Settings** tab in the web UI.

## Environment Variables

| Name | Description | Default |
|------|-------------|---------|
| `CONFIG_PATH` | Path to config YAML file | `config/config.yaml` |

## Tech Stack

- **Backend:** Python 3.11, FastAPI, uvicorn
- **Database:** SQLite (via aiosqlite)
- **Frontend:** Vanilla JS (embedded single HTML file)
- **OScam communication:** httpx (async HTTP to WebIF API)
- **Scheduling:** APScheduler

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m oscam_monitor.main
```

Open http://localhost:8099

## License

MIT
