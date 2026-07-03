"""Main daemon entry point - orchestrates all components."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from .config import get_config, load_config
from .channel_list import load_channel_list, get_channel_list
from .database import get_db, init_db_sync, insert_ecm_event, upsert_channel_mapping, upsert_discovered_user
from .channel_resolver import resolve_channel_for_user
from .log_parser import CardSerialEvent, EcmEvent, EntitlementError, parse_line, parse_log_stream
from .mapping_engine import MappingEngine
from .oscam_api import OscamApiClient
from .serial_monitor import SerialMonitor
from .stats_engine import StatsEngine
from .viewing_tracker import ViewingTracker
from .web import app, set_engines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("oscam_monitor")


class OscamMonitorDaemon:
    """Main daemon that orchestrates all monitoring components."""

    def __init__(self):
        self.config = load_config()
        self.mapping_engine = MappingEngine()
        self.stats_engine = StatsEngine()
        self.serial_monitor = SerialMonitor()
        self.viewing_tracker = ViewingTracker()
        self._running = True
        self._db = None
        self._api_clients: dict[str, OscamApiClient] = {}
        # Track last seen log lines per server to avoid reprocessing
        self._last_log_lines: dict[str, set[str]] = {}

    async def start(self):
        """Start the daemon."""
        logger.info("Starting OScam Monitor daemon...")

        # Initialize database
        init_db_sync()
        self._db = await get_db()

        # Load config into engines
        self.mapping_engine.reload_config()
        self.stats_engine.reload_config()
        self.serial_monitor.reload_config()

        # Load channel list (CAID:SID → channel name + provider)
        load_channel_list()

        # Create API client for the server
        if self.config.server:
            server = self.config.server
            self._api_clients[server.name] = OscamApiClient(
                host=server.host,
                port=server.port,
                username=server.username,
                password=server.password,
            )
            self._last_log_lines[server.name] = set()

        # Register engines with web app
        set_engines(self.stats_engine, self.mapping_engine, self.serial_monitor, self._api_clients, self.viewing_tracker)

        # Start all tasks
        tasks = []

        # Start log polling for the configured server
        if self.config.server:
            server = self.config.server
            if server.log_source == "api":
                # Poll logs from OScam WebIF API
                task = asyncio.create_task(
                    self._poll_logs_from_api(server.name),
                    name=f"api-poller-{server.name}",
                )
                tasks.append(task)
                logger.info(f"Started API log poller for server '{server.name}' ({server.host}:{server.port})")
            else:
                # Tail local log file
                log_path = Path(server.log_source)
                task = asyncio.create_task(
                    parse_log_stream(
                        log_path=log_path,
                        server_name=server.name,
                        on_ecm=self._handle_ecm,
                        on_entitlement_error=self._handle_entitlement_error,
                        on_serial=self._handle_serial,
                    ),
                    name=f"file-parser-{server.name}",
                )
                tasks.append(task)
                logger.info(f"Started file log parser for server '{server.name}' ({log_path})")

        # Start reader info poller (periodic)
        tasks.append(asyncio.create_task(self._reader_poll_loop(), name="reader-poller"))

        # Start session flusher (periodic)
        tasks.append(asyncio.create_task(self._session_flush_loop(), name="session-flusher"))

        # Start mapping persistence (periodic)
        tasks.append(asyncio.create_task(self._mapping_persist_loop(), name="mapping-persist"))

        # Start web server
        web_config = uvicorn.Config(
            app,
            host=self.config.web.host,
            port=self.config.web.port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(web_config)
        tasks.append(asyncio.create_task(server.serve(), name="web-server"))

        logger.info(f"Web dashboard at http://{self.config.web.host}:{self.config.web.port}")
        logger.info("OScam Monitor daemon started successfully")

        # Wait for all tasks (they run forever until cancelled)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Daemon shutting down...")
        finally:
            if self._db:
                await self._db.close()

    # --- API-based log polling ---

    async def _poll_logs_from_api(self, server_name: str, poll_interval: float = 5.0) -> None:
        """Poll OScam WebIF API for new log lines and parse them."""
        api_client = self._api_clients[server_name]

        # Wait for first successful connection
        while self._running:
            connected = await api_client.is_connected()
            if connected:
                logger.info(f"Connected to OScam server '{server_name}'")
                break
            logger.warning(f"Cannot reach OScam server '{server_name}', retrying in 10s...")
            await asyncio.sleep(10)

        while self._running:
            try:
                log_lines = await api_client.get_log()

                # Process only new lines (dedup by content)
                seen = self._last_log_lines[server_name]
                new_lines = [line for line in log_lines if line not in seen]

                # Keep last 500 lines for dedup
                self._last_log_lines[server_name] = set(log_lines[-500:]) if log_lines else set()

                for line in new_lines:
                    event = parse_line(line)
                    if event is None:
                        continue
                    if isinstance(event, EcmEvent):
                        self._handle_ecm(server_name, event)
                    elif isinstance(event, EntitlementError):
                        self._handle_entitlement_error(server_name, event)
                    elif isinstance(event, CardSerialEvent):
                        self._handle_serial(server_name, event)

                # Also pull active clients directly from status API for live view
                # and resolve channel names from receivers to build SID mappings
                status = await api_client.get_status()
                for client in status.get("clients", []):
                    if not client.username or not client.caid or not client.sid:
                        continue
                    if client.caid == "0000" or client.sid == "0000":
                        continue

                    await upsert_discovered_user(self._db, server=server_name, username=client.username)

                    # Try to resolve channel from the user's mapped receiver
                    channel_info = await resolve_channel_for_user(
                        client.username, get_config().user_device_map
                    )
                    if channel_info and channel_info.channel_name:
                        # Verify the SID matches what OScam says
                        receiver_sid = channel_info.sid
                        if receiver_sid and receiver_sid.upper() == client.sid.upper():
                            # Confirmed match — store the mapping
                            await upsert_channel_mapping(
                                self._db,
                                caid=client.caid.upper(),
                                sid=client.sid.upper(),
                                channel_name=channel_info.channel_name,
                            )
                            logger.debug(
                                f"Mapped {client.caid}:{client.sid} → {channel_info.channel_name} "
                                f"(via {client.username}'s receiver)"
                            )
                        else:
                            logger.debug(
                                f"SID mismatch for {client.username}: "
                                f"OScam={client.sid}, receiver={receiver_sid}"
                            )
                    elif channel_info:
                        logger.debug(f"Receiver for {client.username} returned no channel name")
                    else:
                        logger.debug(f"No channel_info for {client.username}")

                    # Update viewing tracker (for stats)
                    resolved_name = None
                    if channel_info and channel_info.channel_name:
                        resolved_name = channel_info.channel_name
                    elif hasattr(client, 'channel_name') and client.channel_name and client.channel_name.lower() != "unknown":
                        resolved_name = client.channel_name
                    await self.viewing_tracker.update(
                        username=client.username,
                        caid=client.caid.upper(),
                        sid=client.sid.upper(),
                        channel_name=resolved_name,
                        db=self._db,
                    )

                # Flush inactive users from viewing tracker
                await self.viewing_tracker.flush_inactive(self._db)

            except Exception as e:
                logger.error(f"Error polling logs from '{server_name}': {e}")

            await asyncio.sleep(poll_interval)

    # --- Reader polling ---

    async def _reader_poll_loop(self, interval: float = 60.0) -> None:
        """Periodically poll reader info and entitlements from all servers."""
        while self._running:
            for server_name, api_client in self._api_clients.items():
                try:
                    readers = await api_client.get_reader_info_with_entitlements()
                    # Store reader info for the web dashboard
                    if not hasattr(self, '_reader_cache'):
                        self._reader_cache = {}
                    self._reader_cache[server_name] = readers

                    # Check serials
                    for reader in readers:
                        if reader.card_serial:
                            event = CardSerialEvent(
                                timestamp=datetime.now(),
                                reader=reader.label,
                                serial=reader.card_serial,
                            )
                            await self.serial_monitor.process_serial_event(server_name, event, self._db)

                except Exception as e:
                    logger.error(f"Error polling readers from '{server_name}': {e}")

            await asyncio.sleep(interval)

    # --- Event handlers ---

    def _handle_ecm(self, server_name: str, event: EcmEvent) -> None:
        """Handle an ECM event from the log parser (sync callback, schedules async work)."""
        asyncio.create_task(self._process_ecm_async(server_name, event))

    def _handle_entitlement_error(self, server_name: str, event: EntitlementError) -> None:
        """Handle a no-entitlement error."""
        self.mapping_engine.process_entitlement_error(event)

    def _handle_serial(self, server_name: str, event: CardSerialEvent) -> None:
        """Handle a card serial event."""
        asyncio.create_task(self._process_serial_async(server_name, event))

    async def _process_ecm_async(self, server_name: str, event: EcmEvent) -> None:
        """Process ECM event asynchronously."""
        try:
            # Track discovered user
            await upsert_discovered_user(self._db, server=server_name, username=event.username)

            # Try to resolve channel name from receiver
            channel_name = None
            if event.success:
                channel_info = await resolve_channel_for_user(
                    event.username, get_config().user_device_map
                )
                if channel_info and channel_info.sid == event.sid:
                    channel_name = channel_info.channel_name

            # Fallback: look up in static channel list
            if not channel_name:
                cl = get_channel_list()
                channel_name = cl.get_channel_name(event.caid, event.sid)

            # Update mapping engine
            state = self.mapping_engine.process_ecm(event, channel_name=channel_name)

            # Update stats engine
            self.stats_engine.process_ecm(
                server_name,
                event,
                channel_name=channel_name or state.channel_name,
                country_tag=state.country_tag,
            )

            # Store ECM event in DB
            await insert_ecm_event(
                self._db,
                timestamp=event.timestamp.isoformat(),
                server=server_name,
                username=event.username,
                caid=event.caid,
                sid=event.sid,
                channel_name=channel_name or state.channel_name,
                reader=event.reader,
                success=event.success,
            )
        except Exception as e:
            logger.error(f"Error processing ECM event: {e}")

    async def _process_serial_async(self, server_name: str, event: CardSerialEvent) -> None:
        """Process card serial event asynchronously."""
        try:
            await self.serial_monitor.process_serial_event(server_name, event, self._db)
        except Exception as e:
            logger.error(f"Error processing serial event: {e}")

    # --- Periodic tasks ---

    async def _session_flush_loop(self) -> None:
        """Periodically flush expired sessions to the database."""
        while self._running:
            await asyncio.sleep(30)  # Check every 30 seconds
            try:
                await self.stats_engine.flush_expired_sessions(self._db)
            except Exception as e:
                logger.error(f"Error flushing sessions: {e}")

    async def _mapping_persist_loop(self) -> None:
        """Periodically persist mapping state to database."""
        while self._running:
            await asyncio.sleep(60)  # Persist every 60 seconds
            try:
                await self.mapping_engine.persist_state(self._db)
            except Exception as e:
                logger.error(f"Error persisting mappings: {e}")


# Expose reader cache for the web API
_daemon_instance: OscamMonitorDaemon | None = None


def get_daemon() -> OscamMonitorDaemon | None:
    return _daemon_instance


def main():
    """Entry point."""
    global _daemon_instance
    daemon = OscamMonitorDaemon()
    _daemon_instance = daemon

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        daemon._running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    asyncio.run(daemon.start())


if __name__ == "__main__":
    main()
