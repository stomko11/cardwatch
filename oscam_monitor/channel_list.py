"""Channel list - loads CAID:SID → channel name mappings from file."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# CAID → Provider name mapping
CAID_PROVIDERS: dict[str, str] = {
    "0B00": "AntikSAT",
    "0624": "Skylink",
    "0668": "RTVS",
}


def get_provider_by_caid(caid: str) -> str | None:
    """Get provider name for a CAID. Returns None if unknown."""
    return CAID_PROVIDERS.get(caid.upper())


class ChannelList:
    """Loads and provides lookup for CAID:SID → channel name from a file."""

    def __init__(self):
        # Key: "CAID:SID" (uppercase hex), Value: {"name": str, "provider": str}
        self._channels: dict[str, dict] = {}

    def load(self, path: Path | str) -> int:
        """
        Load channel list from file.
        Format per line: SID:CAID[@PROVID]|Channel Name|||Provider
        Returns number of channels loaded.
        """
        path = Path(path)
        if not path.exists():
            logger.warning(f"Channel list file not found: {path}")
            return 0

        count = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                try:
                    entry = self._parse_line(line)
                    if entry:
                        key = f"{entry['caid']}:{entry['sid']}"
                        self._channels[key] = {
                            "name": entry["name"],
                            "provider": entry["provider"],
                        }
                        count += 1
                except Exception as e:
                    logger.debug(f"Failed to parse channel line: {line!r} — {e}")

        logger.info(f"Loaded {count} channel mappings from {path}")
        return count

    @staticmethod
    def _parse_line(line: str) -> dict | None:
        """
        Parse a single line from the channel list.
        Format: SID:CAID[@PROVID]|Channel Name|||Provider
        Example: 0A9A:0B00@000000|Eroxxx HD|||AntikSAT
        """
        # Split on | to get parts
        parts = line.split("|")
        if len(parts) < 2:
            return None

        # First part: SID:CAID[@PROVID]
        id_part = parts[0].strip()
        channel_name = parts[1].strip()
        provider = parts[-1].strip() if len(parts) >= 4 else ""

        # Parse SID:CAID[@PROVID]
        # Remove @PROVID if present
        if "@" in id_part:
            id_part = id_part.split("@")[0]

        # Split SID:CAID
        if ":" not in id_part:
            return None

        sid, caid = id_part.split(":", 1)

        return {
            "sid": sid.upper(),
            "caid": caid.upper(),
            "name": channel_name,
            "provider": provider or get_provider_by_caid(caid) or "unknown",
        }

    def lookup(self, caid: str, sid: str) -> dict | None:
        """
        Look up channel info by CAID and SID.
        Returns {"name": str, "provider": str} or None.
        """
        key = f"{caid.upper()}:{sid.upper()}"
        return self._channels.get(key)

    def get_channel_name(self, caid: str, sid: str) -> str | None:
        """Get just the channel name for a CAID:SID pair."""
        info = self.lookup(caid, sid)
        return info["name"] if info else None

    def get_provider(self, caid: str, sid: str) -> str | None:
        """Get provider for a CAID:SID pair. Falls back to CAID-based lookup."""
        info = self.lookup(caid, sid)
        if info:
            return info["provider"]
        return get_provider_by_caid(caid)

    @property
    def count(self) -> int:
        return len(self._channels)


# Global instance
_channel_list: ChannelList | None = None


def get_channel_list() -> ChannelList:
    """Get the global channel list instance."""
    global _channel_list
    if _channel_list is None:
        _channel_list = ChannelList()
    return _channel_list


def load_channel_list(path: Path | str | None = None) -> ChannelList:
    """Load channel list from file. Uses default path if none given."""
    global _channel_list
    if _channel_list is None:
        _channel_list = ChannelList()

    if path is None:
        # Default path: config/channels.csv relative to project root
        path = Path(__file__).parent.parent / "config" / "channels.csv"

    _channel_list.load(path)
    return _channel_list
