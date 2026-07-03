"""Simple viewing tracker - records what users watch if they stay for at least 1 minute."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

logger = logging.getLogger(__name__)

MIN_DURATION_SECONDS = 60  # Minimum time on a channel to count as "watching"


@dataclass
class UserState:
    """Tracks what a user is currently watching."""
    username: str
    caid: str
    sid: str
    channel_name: str | None
    first_seen: datetime  # When we first saw them on this channel
    last_seen: datetime   # Last time we confirmed they're still here
    recorded: bool = False  # Whether we already wrote the session start to DB


class ViewingTracker:
    """
    Simple approach: track per-user what they're watching.
    If they stay on the same CAID:SID for >= 60 seconds, record it.
    When they switch or go offline, close the session.
    """

    def __init__(self):
        self._states: dict[str, UserState] = {}  # username → current state

    async def update(self, username: str, caid: str, sid: str,
                     channel_name: str | None, db: aiosqlite.Connection) -> None:
        """Called every poll cycle with what a user is currently decoding."""
        now = datetime.now()
        key = username

        if key in self._states:
            state = self._states[key]
            if state.caid == caid and state.sid == sid:
                # Same channel — update last_seen
                state.last_seen = now
                if channel_name:
                    state.channel_name = channel_name

                # Check if we've been here long enough to record
                if not state.recorded and (now - state.first_seen).total_seconds() >= MIN_DURATION_SECONDS:
                    state.recorded = True
                    # Don't write to DB yet — we'll write when they leave
            else:
                # Changed channel — close previous session if it was long enough
                await self._close_session(state, db)
                # Start tracking new channel
                self._states[key] = UserState(
                    username=username, caid=caid, sid=sid,
                    channel_name=channel_name,
                    first_seen=now, last_seen=now,
                )
        else:
            # New user
            self._states[key] = UserState(
                username=username, caid=caid, sid=sid,
                channel_name=channel_name,
                first_seen=now, last_seen=now,
            )

    async def flush_inactive(self, db: aiosqlite.Connection, timeout_seconds: int = 90) -> None:
        """Close sessions for users who haven't been seen recently."""
        now = datetime.now()
        to_remove = []
        for key, state in self._states.items():
            if (now - state.last_seen).total_seconds() > timeout_seconds:
                await self._close_session(state, db)
                to_remove.append(key)
        for key in to_remove:
            del self._states[key]

    async def _close_session(self, state: UserState, db: aiosqlite.Connection) -> None:
        """Write a completed session to DB if it was long enough."""
        duration = int((state.last_seen - state.first_seen).total_seconds())
        if duration < MIN_DURATION_SECONDS:
            return  # Too short — user was just flipping channels

        await db.execute(
            """INSERT INTO watch_sessions 
               (server, username, channel_name, caid, sid, start_time, end_time, duration_seconds, country_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "",  # server (not critical for stats)
                state.username,
                state.channel_name,
                state.caid,
                state.sid,
                state.first_seen.isoformat(),
                state.last_seen.isoformat(),
                duration,
                "unknown",
            ),
        )
        await db.commit()
        logger.debug(
            f"Session recorded: {state.username} watched {state.channel_name or state.sid} "
            f"for {duration}s"
        )
