"""Mapping engine - builds CAID:SID → channel mappings and tags SK/CZ country."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import aiosqlite

from .config import get_config
from .database import upsert_channel_mapping
from .log_parser import EcmEvent, EntitlementError

logger = logging.getLogger(__name__)


@dataclass
class SidState:
    """Tracks state for a CAID:SID pair to determine country tag."""

    caid: str
    sid: str
    channel_name: str | None = None
    succeeded_readers: set[str] = field(default_factory=set)
    failed_readers: set[str] = field(default_factory=set)
    country_tag: str = "unknown"
    last_seen: datetime = field(default_factory=datetime.now)


class MappingEngine:
    """
    Builds and maintains CAID:SID → channel name mappings.

    Country tagging logic:
    - If a SID decodes successfully on a reader tagged as SK but fails (no entitlement)
      on a reader tagged CZ → it's a SK channel (and vice versa).
    - If it decodes on both → unknown (shared/common channel).
    - If it only ever appears on one reader → tag based on that reader.
    """

    def __init__(self):
        self._state: dict[str, SidState] = {}  # key: "CAID:SID"
        self._reader_country_map: dict[str, str] = {}

    def reload_config(self) -> None:
        """Reload reader-country mapping from config."""
        cfg = get_config()
        self._reader_country_map = dict(cfg.readers)
        logger.info(f"Mapping engine loaded {len(self._reader_country_map)} reader-country tags")

    def _key(self, caid: str, sid: str) -> str:
        return f"{caid}:{sid}"

    def _determine_country(self, state: SidState) -> str:
        """
        Determine country based on which readers succeeded/failed.

        Logic:
        - Find countries of readers that succeeded
        - Find countries of readers that failed (no entitlement)
        - If succeeded on SK reader and failed on CZ reader → SK
        - If succeeded on CZ reader and failed on SK reader → CZ
        - If succeeded on both → unknown (common channel)
        - If only one reader seen → tag it based on that reader
        """
        if not self._reader_country_map:
            return "unknown"

        success_countries = set()
        fail_countries = set()

        for reader in state.succeeded_readers:
            country = self._reader_country_map.get(reader)
            if country:
                success_countries.add(country)

        for reader in state.failed_readers:
            country = self._reader_country_map.get(reader)
            if country:
                fail_countries.add(country)

        # Clear-cut case: succeeds on one country's card, fails on the other
        if "SK" in success_countries and "CZ" in fail_countries and "CZ" not in success_countries:
            return "SK"
        if "CZ" in success_countries and "SK" in fail_countries and "SK" not in success_countries:
            return "CZ"

        # Succeeds on both — it's a shared/common channel
        if "SK" in success_countries and "CZ" in success_countries:
            return "shared"

        # Only one country reader has seen it, and it succeeded
        if len(success_countries) == 1 and not fail_countries:
            # Don't tag yet — might just not have been tried on the other reader
            return "unknown"

        return "unknown"

    def process_ecm(self, event: EcmEvent, channel_name: str | None = None) -> SidState:
        """Process an ECM event and update mapping state."""
        key = self._key(event.caid, event.sid)

        if key not in self._state:
            self._state[key] = SidState(caid=event.caid, sid=event.sid)

        state = self._state[key]
        state.last_seen = event.timestamp

        if channel_name:
            state.channel_name = channel_name

        if event.reader:
            if event.success:
                state.succeeded_readers.add(event.reader)
            else:
                state.failed_readers.add(event.reader)

        # Re-evaluate country tag
        state.country_tag = self._determine_country(state)

        return state

    def process_entitlement_error(self, error: EntitlementError) -> SidState:
        """Process a no-entitlement error — this reader can NOT decode this SID."""
        key = self._key(error.caid, error.sid)

        if key not in self._state:
            self._state[key] = SidState(caid=error.caid, sid=error.sid)

        state = self._state[key]
        state.failed_readers.add(error.reader)
        state.last_seen = error.timestamp

        # Re-evaluate country tag
        state.country_tag = self._determine_country(state)

        return state

    async def persist_state(self, db: aiosqlite.Connection) -> None:
        """Persist current mapping state to database."""
        for state in self._state.values():
            await upsert_channel_mapping(
                db,
                caid=state.caid,
                sid=state.sid,
                channel_name=state.channel_name,
                country_tag=state.country_tag,
            )

    def get_all_mappings(self) -> list[dict]:
        """Get all current mappings as dicts."""
        return [
            {
                "caid": s.caid,
                "sid": s.sid,
                "channel_name": s.channel_name,
                "country_tag": s.country_tag,
                "succeeded_readers": list(s.succeeded_readers),
                "failed_readers": list(s.failed_readers),
                "last_seen": s.last_seen.isoformat(),
            }
            for s in self._state.values()
        ]

    def export_oscam_services(self) -> str:
        """
        Export mappings in oscam.services format.

        Format:
        [channel_name]
        caid = XXXX
        srvid = XXXX
        """
        lines = []
        for state in sorted(self._state.values(), key=lambda s: s.channel_name or ""):
            if not state.channel_name:
                continue
            tag = f" ({state.country_tag})" if state.country_tag != "unknown" else ""
            lines.append(f"[{state.channel_name}{tag}]")
            lines.append(f"caid = {state.caid}")
            lines.append(f"srvid = {state.sid}")
            lines.append("")

        return "\n".join(lines)
