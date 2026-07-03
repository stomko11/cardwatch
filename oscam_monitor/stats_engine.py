"""Stats engine - tracks viewing sessions and provides aggregated statistics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiosqlite

from .config import get_config
from .log_parser import EcmEvent

logger = logging.getLogger(__name__)


@dataclass
class ActiveSession:
    """An active viewing session (user watching a channel)."""

    username: str
    server: str
    caid: str
    sid: str
    channel_name: str | None
    country_tag: str
    start_time: datetime
    last_ecm_time: datetime
    ecm_count: int = 0


class StatsEngine:
    """
    Tracks viewing sessions and computes statistics.

    A "session" = continuous ECM stream from same user for same SID.
    If no ECM for that user+SID within session_timeout → session ends.
    """

    def __init__(self):
        self._active_sessions: dict[str, ActiveSession] = {}  # key: "server:user:caid:sid"
        self._session_timeout = 60  # seconds

    def reload_config(self) -> None:
        """Reload timeout from config."""
        cfg = get_config()
        self._session_timeout = cfg.stats.session_timeout_seconds

    def _session_key(self, server: str, username: str, caid: str, sid: str) -> str:
        return f"{server}:{username}:{caid}:{sid}"

    def process_ecm(
        self,
        server: str,
        event: EcmEvent,
        channel_name: str | None = None,
        country_tag: str = "unknown",
    ) -> ActiveSession:
        """
        Process an ECM event for session tracking.
        Returns the active session (new or continued).
        """
        if not event.success:
            # Don't count failed decodes as viewing
            return None

        key = self._session_key(server, event.username, event.caid, event.sid)

        if key in self._active_sessions:
            session = self._active_sessions[key]
            session.last_ecm_time = event.timestamp
            session.ecm_count += 1
            if channel_name:
                session.channel_name = channel_name
            return session
        else:
            # New session
            session = ActiveSession(
                username=event.username,
                server=server,
                caid=event.caid,
                sid=event.sid,
                channel_name=channel_name,
                country_tag=country_tag,
                start_time=event.timestamp,
                last_ecm_time=event.timestamp,
                ecm_count=1,
            )
            self._active_sessions[key] = session
            return session

    async def flush_expired_sessions(self, db: aiosqlite.Connection, now: datetime | None = None) -> list[ActiveSession]:
        """
        Check for expired sessions and persist them to DB.
        Returns list of sessions that were closed.
        """
        now = now or datetime.now()
        timeout = timedelta(seconds=self._session_timeout)
        expired = []

        keys_to_remove = []
        for key, session in self._active_sessions.items():
            if now - session.last_ecm_time > timeout:
                keys_to_remove.append(key)
                expired.append(session)

        for key in keys_to_remove:
            session = self._active_sessions.pop(key)
            duration = int((session.last_ecm_time - session.start_time).total_seconds())
            await db.execute(
                """INSERT INTO watch_sessions
                   (server, username, channel_name, caid, sid, start_time, end_time, duration_seconds, country_tag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.server,
                    session.username,
                    session.channel_name,
                    session.caid,
                    session.sid,
                    session.start_time.isoformat(),
                    session.last_ecm_time.isoformat(),
                    duration,
                    session.country_tag,
                ),
            )
            await db.commit()

        if expired:
            logger.debug(f"Flushed {len(expired)} expired sessions")

        return expired

    def get_active_sessions(self) -> list[dict]:
        """Get all currently active sessions."""
        now = datetime.now()
        timeout = timedelta(seconds=self._session_timeout)
        active = []
        for session in self._active_sessions.values():
            # Only include if not yet timed out
            if now - session.last_ecm_time <= timeout:
                duration = int((now - session.start_time).total_seconds())
                active.append({
                    "username": session.username,
                    "server": session.server,
                    "channel_name": session.channel_name,
                    "caid": session.caid,
                    "sid": session.sid,
                    "country_tag": session.country_tag,
                    "start_time": session.start_time.isoformat(),
                    "duration_seconds": duration,
                    "ecm_count": session.ecm_count,
                })
        return active


# --- Query functions for stats API ---


async def get_user_stats(db: aiosqlite.Connection, username: str, days: int = 30) -> dict:
    """Get viewing stats for a specific user."""
    since = (datetime.now() - timedelta(days=days)).isoformat()

    # Total watch time
    cursor = await db.execute(
        "SELECT COALESCE(SUM(duration_seconds), 0) FROM watch_sessions WHERE username = ? AND start_time > ?",
        (username, since),
    )
    total_seconds = (await cursor.fetchone())[0]

    # Channel breakdown
    cursor = await db.execute(
        """SELECT channel_name, country_tag, SUM(duration_seconds) as total, COUNT(*) as sessions
           FROM watch_sessions
           WHERE username = ? AND start_time > ? AND channel_name IS NOT NULL
           GROUP BY channel_name
           ORDER BY total DESC
           LIMIT 20""",
        (username, since),
    )
    channels = [dict(row) for row in await cursor.fetchall()]

    # Daily totals
    cursor = await db.execute(
        """SELECT DATE(start_time) as day, SUM(duration_seconds) as total
           FROM watch_sessions
           WHERE username = ? AND start_time > ?
           GROUP BY DATE(start_time)
           ORDER BY day""",
        (username, since),
    )
    daily = [dict(row) for row in await cursor.fetchall()]

    # Peak hours
    cursor = await db.execute(
        """SELECT CAST(strftime('%H', start_time) AS INTEGER) as hour, SUM(duration_seconds) as total
           FROM watch_sessions
           WHERE username = ? AND start_time > ?
           GROUP BY hour
           ORDER BY hour""",
        (username, since),
    )
    hourly = [dict(row) for row in await cursor.fetchall()]

    return {
        "username": username,
        "period_days": days,
        "total_seconds": total_seconds,
        "total_hours": round(total_seconds / 3600, 1),
        "top_channels": channels,
        "daily_totals": daily,
        "hourly_distribution": hourly,
    }


async def get_global_stats(db: aiosqlite.Connection, days: int = 30) -> dict:
    """Get global viewing statistics."""
    since = (datetime.now() - timedelta(days=days)).isoformat()

    # Top channels
    cursor = await db.execute(
        """SELECT channel_name, country_tag, SUM(duration_seconds) as total, COUNT(DISTINCT username) as users
           FROM watch_sessions
           WHERE start_time > ? AND channel_name IS NOT NULL
           GROUP BY channel_name
           ORDER BY total DESC
           LIMIT 20""",
        (since,),
    )
    top_channels = [dict(row) for row in await cursor.fetchall()]

    # Per-user totals
    cursor = await db.execute(
        """SELECT username, SUM(duration_seconds) as total, COUNT(*) as sessions
           FROM watch_sessions
           WHERE start_time > ?
           GROUP BY username
           ORDER BY total DESC""",
        (since,),
    )
    user_totals = [dict(row) for row in await cursor.fetchall()]

    # SK vs CZ split
    cursor = await db.execute(
        """SELECT country_tag, SUM(duration_seconds) as total
           FROM watch_sessions
           WHERE start_time > ? AND country_tag IN ('SK', 'CZ', 'shared')
           GROUP BY country_tag""",
        (since,),
    )
    country_split = {row["country_tag"]: row["total"] for row in await cursor.fetchall()}

    # Daily activity
    cursor = await db.execute(
        """SELECT DATE(start_time) as day, SUM(duration_seconds) as total, COUNT(DISTINCT username) as users
           FROM watch_sessions
           WHERE start_time > ?
           GROUP BY DATE(start_time)
           ORDER BY day""",
        (since,),
    )
    daily = [dict(row) for row in await cursor.fetchall()]

    return {
        "period_days": days,
        "top_channels": top_channels,
        "user_totals": user_totals,
        "country_split": country_split,
        "daily_activity": daily,
    }


async def get_live_activity(db: aiosqlite.Connection) -> dict:
    """Get recent activity (last hour) from DB for when active sessions aren't available."""
    since = (datetime.now() - timedelta(hours=1)).isoformat()

    cursor = await db.execute(
        """SELECT username, channel_name, caid, sid, country_tag, MAX(end_time) as last_seen
           FROM watch_sessions
           WHERE end_time > ?
           GROUP BY username
           ORDER BY last_seen DESC""",
        (since,),
    )
    recent = [dict(row) for row in await cursor.fetchall()]
    return {"recent_sessions": recent}


async def get_card_stats(db: aiosqlite.Connection, days: int = 30) -> dict:
    """Get per-card (reader) statistics.

    'Busy hours' = distinct clock-hours where the card decoded at least 1 ECM.
    If 5 users use a card simultaneously for 1 hour, that's 1 busy hour.
    """
    since = (datetime.now() - timedelta(days=days)).isoformat()

    # Total busy hours per card (distinct hour-slots with activity)
    cursor = await db.execute(
        """SELECT reader,
                  COUNT(DISTINCT strftime('%Y-%m-%d %H', timestamp)) as busy_hours
           FROM ecm_events
           WHERE success = 1 AND timestamp > ? AND reader IS NOT NULL
           GROUP BY reader
           ORDER BY busy_hours DESC""",
        (since,),
    )
    card_totals = [dict(row) for row in await cursor.fetchall()]

    # Daily busy hours per card
    cursor = await db.execute(
        """SELECT DATE(timestamp) as day,
                  reader,
                  COUNT(DISTINCT strftime('%Y-%m-%d %H', timestamp)) as busy_hours
           FROM ecm_events
           WHERE success = 1 AND timestamp > ? AND reader IS NOT NULL
           GROUP BY day, reader
           ORDER BY day""",
        (since,),
    )
    daily_busy = [dict(row) for row in await cursor.fetchall()]

    # Daily unique users per card
    cursor = await db.execute(
        """SELECT DATE(timestamp) as day,
                  reader,
                  COUNT(DISTINCT username) as unique_users
           FROM ecm_events
           WHERE success = 1 AND timestamp > ? AND reader IS NOT NULL
           GROUP BY day, reader
           ORDER BY day""",
        (since,),
    )
    daily_users = [dict(row) for row in await cursor.fetchall()]

    # Daily unique channels (SIDs) per card
    cursor = await db.execute(
        """SELECT DATE(timestamp) as day,
                  reader,
                  COUNT(DISTINCT sid) as unique_channels
           FROM ecm_events
           WHERE success = 1 AND timestamp > ? AND reader IS NOT NULL
           GROUP BY day, reader
           ORDER BY day""",
        (since,),
    )
    daily_channels = [dict(row) for row in await cursor.fetchall()]

    return {
        "period_days": days,
        "card_totals": card_totals,
        "daily_busy_hours": daily_busy,
        "daily_unique_users": daily_users,
        "daily_unique_channels": daily_channels,
    }
