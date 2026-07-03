"""Serial monitor - tracks card serials and sends Pushover alerts on changes."""

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite
import httpx

from .config import get_config
from .database import upsert_card_serial
from .log_parser import CardSerialEvent

logger = logging.getLogger(__name__)


class PushoverNotifier:
    """Sends push notifications via Pushover API."""

    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, app_token: str, user_key: str):
        self.app_token = app_token
        self.user_key = user_key

    async def send(self, title: str, message: str, priority: int = 0) -> bool:
        """Send a Pushover notification."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.API_URL,
                    data={
                        "token": self.app_token,
                        "user": self.user_key,
                        "title": title,
                        "message": message,
                        "priority": priority,
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Pushover notification sent: {title}")
                    return True
                else:
                    logger.warning(f"Pushover failed ({resp.status_code}): {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Pushover error: {e}")
            return False


class SerialMonitor:
    """Monitors card serial numbers and alerts on changes."""

    def __init__(self):
        self._notifier: PushoverNotifier | None = None
        self._known_serials: dict[str, str] = {}  # "server:reader" → serial

    def reload_config(self) -> None:
        """Reload Pushover settings from config."""
        cfg = get_config()
        if cfg.pushover.enabled and cfg.pushover.app_token and cfg.pushover.user_key:
            self._notifier = PushoverNotifier(cfg.pushover.app_token, cfg.pushover.user_key)
            logger.info("Pushover notifications enabled")
        else:
            self._notifier = None
            logger.info("Pushover notifications disabled")

    async def process_serial_event(
        self,
        server: str,
        event: CardSerialEvent,
        db: aiosqlite.Connection,
    ) -> bool:
        """
        Process a card serial event. Returns True if serial changed.
        Sends Pushover alert if serial changed and notifications are enabled.
        """
        key = f"{server}:{event.reader}"
        previous = self._known_serials.get(key)

        # Update DB
        changed = await upsert_card_serial(
            db,
            server=server,
            reader=event.reader,
            serial=event.serial,
        )

        # Also track in memory
        self._known_serials[key] = event.serial

        if previous and previous != event.serial:
            # Serial changed!
            logger.warning(
                f"Card serial changed on {server}/{event.reader}: {previous} → {event.serial}"
            )

            if self._notifier:
                await self._notifier.send(
                    title="⚠️ OScam Card Serial Changed",
                    message=(
                        f"Server: {server}\n"
                        f"Reader: {event.reader}\n"
                        f"Old serial: {previous}\n"
                        f"New serial: {event.serial}\n"
                        f"Time: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                    ),
                    priority=1,  # High priority
                )
            return True

        if not previous and changed:
            # First time seeing this reader — just record it
            logger.info(f"New card serial recorded: {server}/{event.reader} = {event.serial}")

        return False

    def get_known_serials(self) -> dict[str, str]:
        """Get all known card serials."""
        return dict(self._known_serials)
