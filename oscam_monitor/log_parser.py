"""OScam log parser - tails log files and emits structured events."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Callable

logger = logging.getLogger(__name__)


@dataclass
class EcmEvent:
    """Parsed ECM request event."""

    timestamp: datetime
    username: str
    caid: str
    sid: str
    reader: str | None = None
    success: bool = True
    ecm_time: int | None = None  # ms


@dataclass
class EntitlementError:
    """No entitlement error event."""

    timestamp: datetime
    username: str
    caid: str
    sid: str
    reader: str


@dataclass
class CardSerialEvent:
    """Card serial detected event."""

    timestamp: datetime
    reader: str
    serial: str


# OScam log line patterns
# Actual format: 2026/07/02 21:11:36 055FBEE7 c      (ecm) stomkohs2 (0B00@000000/0000/04C8/8A:474D...): found (381 ms) by Antik - RTVS Sport (lg)
# Structure: CAID@PROVID/xxxx/SID/...
ECM_PATTERN = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"[0-9A-Fa-f]+\s+[a-z]\s+"
    r".*?"
    r"(?P<user>[\w-]+)\s+\("
    r"(?P<caid>[0-9A-Fa-f]{4})"
    r"[@&][0-9A-Fa-f]+/"
    r"[0-9A-Fa-f]+/"
    r"(?P<sid>[0-9A-Fa-f]{4})"
    r"/[^)]*\):\s+"
    r"(?P<result>found|not found|timeout|rejected)"
    r"(?:\s+\w+)?"  # "group" after rejected
    r"(?:\s+\((?P<ecm_time>\d+)\s*ms\))?"
    r"(?:\s+by\s+(?P<reader>[\w-]+))?"
)

# Alternative pattern for older format: (CAID&PROVID/SID/...)
ECM_PATTERN_ALT = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"[0-9A-Fa-f]+\s+[a-z]\s+"
    r".*?"
    r"(?P<user>[\w-]+)\s+\("
    r"(?P<caid>[0-9A-Fa-f]{4})"
    r"[@&][0-9A-Fa-f]+/"
    r"(?P<sid>[0-9A-Fa-f]{4})"
    r"/[^)]*\):\s+"
    r"(?P<result>found|not found|timeout|rejected)"
    r"(?:\s+\w+)?"
    r"(?:\s+\((?P<ecm_time>\d+)\s*ms\))?"
    r"(?:\s+by\s+(?P<reader>[\w-]+))?"
)

# No entitlement pattern
NO_ENTITLEMENT_PATTERN = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r".*?(?P<reader>\S+):\s+.*?no entitlement.*?"
    r"(?P<caid>[0-9A-Fa-f]{4}).*?"
    r"(?P<sid>[0-9A-Fa-f]{4})"
)

# Card serial pattern
SERIAL_PATTERN = re.compile(
    r"(?P<date>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r".*?(?P<reader>\S+).*?serial\s*[:=]\s*(?P<serial>[0-9A-Fa-f]+)"
)


def parse_timestamp(ts_str: str) -> datetime:
    """Parse OScam timestamp format."""
    try:
        return datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return datetime.now()


def parse_line(line: str) -> EcmEvent | EntitlementError | CardSerialEvent | None:
    """Parse a single OScam log line into a structured event."""
    line = line.strip()
    if not line:
        return None

    # Try ECM pattern first
    match = ECM_PATTERN.search(line)
    if not match:
        match = ECM_PATTERN_ALT.search(line)

    if match:
        groups = match.groupdict()
        ts = parse_timestamp(groups["date"])
        success = groups.get("result") == "found"
        ecm_time = int(groups["ecm_time"]) if groups.get("ecm_time") else None

        return EcmEvent(
            timestamp=ts,
            username=groups["user"],
            caid=groups["caid"].upper(),
            sid=groups["sid"].upper(),
            reader=groups.get("reader"),
            success=success,
            ecm_time=ecm_time,
        )

    # Try no entitlement pattern
    match = NO_ENTITLEMENT_PATTERN.search(line)
    if match:
        groups = match.groupdict()
        return EntitlementError(
            timestamp=parse_timestamp(groups["date"]),
            username=groups.get("user", ""),
            caid=groups["caid"].upper(),
            sid=groups["sid"].upper(),
            reader=groups["reader"],
        )

    # Try serial pattern
    match = SERIAL_PATTERN.search(line)
    if match:
        groups = match.groupdict()
        return CardSerialEvent(
            timestamp=parse_timestamp(groups["date"]),
            reader=groups["reader"],
            serial=groups["serial"].upper(),
        )

    return None


async def tail_file(path: Path, poll_interval: float = 0.5) -> AsyncGenerator[str, None]:
    """Async generator that tails a file, yielding new lines as they appear."""
    # Start at end of file
    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        logger.warning(f"Log file not found, waiting: {path}")
        file_size = 0

    while True:
        try:
            if not path.exists():
                await asyncio.sleep(poll_interval * 4)
                continue

            current_size = path.stat().st_size

            if current_size < file_size:
                # File was rotated/truncated
                logger.info(f"Log file rotated: {path}")
                file_size = 0

            if current_size > file_size:
                with open(path, "r", errors="replace") as f:
                    f.seek(file_size)
                    for line in f:
                        yield line
                    file_size = f.tell()

            await asyncio.sleep(poll_interval)

        except Exception as e:
            logger.error(f"Error tailing {path}: {e}")
            await asyncio.sleep(poll_interval * 4)


async def parse_log_stream(
    log_path: Path,
    server_name: str,
    on_ecm: Callable[[str, EcmEvent], None] | None = None,
    on_entitlement_error: Callable[[str, EntitlementError], None] | None = None,
    on_serial: Callable[[str, CardSerialEvent], None] | None = None,
) -> None:
    """
    Main log parsing loop. Tails the log file and dispatches events via callbacks.

    Args:
        log_path: Path to the OScam log file
        server_name: Name of the server (for multi-server identification)
        on_ecm: Callback for ECM events
        on_entitlement_error: Callback for no-entitlement errors
        on_serial: Callback for card serial events
    """
    logger.info(f"Starting log parser for server '{server_name}' at {log_path}")

    async for line in tail_file(log_path):
        event = parse_line(line)

        if event is None:
            continue

        if isinstance(event, EcmEvent) and on_ecm:
            on_ecm(server_name, event)
        elif isinstance(event, EntitlementError) and on_entitlement_error:
            on_entitlement_error(server_name, event)
        elif isinstance(event, CardSerialEvent) and on_serial:
            on_serial(server_name, event)
