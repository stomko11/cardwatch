"""FastAPI web application - dashboard and settings API."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .channel_list import get_channel_list, get_provider_by_caid
from .config import (
    AppConfig,
    OscamServer,
    ReceiverDevice,
    get_config,
    remove_reader_country,
    remove_server,
    remove_user_device,
    set_reader_country,
    set_server,
    set_user_device,
    update_server,
)
from .database import get_channel_mappings, get_db, get_discovered_users
from .stats_engine import get_card_stats, get_global_stats, get_user_stats

logger = logging.getLogger(__name__)

app = FastAPI(title="OScam Monitor", version="0.1.0")


security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify HTTP Basic Auth credentials."""
    cfg = get_config()
    correct_user = secrets.compare_digest(credentials.username, cfg.web.username)
    correct_pass = secrets.compare_digest(credentials.password, cfg.web.password)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# Apply auth to all routes via middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Skip auth for static files
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    # Check basic auth
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OScam Monitor"'},
        )
    import base64
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
        cfg = get_config()
        if not (secrets.compare_digest(username, cfg.web.username) and
                secrets.compare_digest(password, cfg.web.password)):
            raise ValueError()
    except Exception:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="OScam Monitor"'},
        )
    return await call_next(request)


# Ring buffer for recent log messages (shown in UI)
_log_buffer: list[dict] = []
_LOG_BUFFER_MAX = 200


class UILogHandler(logging.Handler):
    """Captures log messages for the web UI."""

    def emit(self, record):
        from datetime import datetime
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        _log_buffer.append(entry)
        if len(_log_buffer) > _LOG_BUFFER_MAX:
            _log_buffer.pop(0)


# Attach handler to root oscam_monitor logger
_ui_handler = UILogHandler()
_ui_handler.setLevel(logging.INFO)
logging.getLogger("oscam_monitor").addHandler(_ui_handler)

# We'll store references to shared engines here (set by main.py on startup)
_stats_engine = None
_mapping_engine = None
_serial_monitor = None
_api_clients: dict = {}
_viewing_tracker = None


def set_engines(stats_engine, mapping_engine, serial_monitor, api_clients=None, viewing_tracker=None):
    """Set references to shared engine instances (called from main on startup)."""
    global _stats_engine, _mapping_engine, _serial_monitor, _api_clients, _viewing_tracker
    _stats_engine = stats_engine
    _mapping_engine = mapping_engine
    _serial_monitor = serial_monitor
    _viewing_tracker = viewing_tracker
    if api_clients:
        _api_clients = api_clients


# --- Pydantic request models ---


class ServerCreate(BaseModel):
    name: str
    host: str
    port: int = 8888
    log_source: str = "api"
    username: str | None = None
    password: str | None = None


class ServerUpdate(BaseModel):
    host: str | None = None
    port: int | None = None
    log_source: str | None = None
    username: str | None = None
    password: str | None = None


class UserDeviceMapping(BaseModel):
    username: str
    receiver_ip: str
    receiver_port: int = 80
    receiver_username: str | None = None
    receiver_password: str | None = None


class ReaderCountry(BaseModel):
    reader_name: str
    country: str  # SK or CZ


# --- Dashboard API ---


@app.get("/api/live")
async def get_live():
    """Get current live activity — who's watching what right now.

    Uses OScam status API directly for real-time accuracy (one entry per user).
    Enriches with channel names from DB mappings and channel list.
    """
    sessions = []
    seen_users = set()
    channel_list = get_channel_list()

    # Get active clients from OScam status API
    for server_name, api_client in _api_clients.items():
        try:
            status = await api_client.get_status()
            for client in status.get("clients", []):
                if not client.username or client.username in seen_users:
                    continue
                caid = client.caid.upper() if client.caid else ""
                sid = client.sid.upper() if client.sid else ""
                if not caid or caid == "0000":
                    continue

                seen_users.add(client.username)

                # Resolve channel name: OScam status → DB mapping → channel list
                channel_name = client.channel_name if hasattr(client, 'channel_name') and client.channel_name and client.channel_name.lower() != "unknown" else None
                if not channel_name and caid and sid:
                    # Try DB mapping
                    db = await get_db()
                    try:
                        cursor = await db.execute(
                            "SELECT channel_name FROM channel_mappings WHERE caid = ? AND sid = ?",
                            (caid, sid),
                        )
                        row = await cursor.fetchone()
                        if row and row["channel_name"]:
                            channel_name = row["channel_name"]
                    finally:
                        await db.close()
                if not channel_name and caid and sid:
                    channel_name = channel_list.get_channel_name(caid, sid)

                provider = get_provider_by_caid(caid)

                sessions.append({
                    "username": client.username,
                    "server": server_name,
                    "channel_name": channel_name,
                    "caid": caid,
                    "sid": sid,
                    "provider": provider,
                    "idle_seconds": client.idle_seconds,
                    "ecm_time_ms": client.last_ecm_time,
                })
        except Exception:
            pass

    return {"active_sessions": sessions}



@app.get("/api/logs")
async def get_logs(limit: int = 50):
    """Get recent application log messages."""
    return {"logs": _log_buffer[-limit:]}


@app.get("/api/stats/global")
async def api_global_stats(days: int = 30):
    """Get global viewing statistics, including currently active sessions."""
    db = await get_db()
    try:
        stats = await get_global_stats(db, days=days)

        # Add currently active sessions from viewing tracker to user totals
        if _viewing_tracker:
            from datetime import datetime
            now = datetime.now()
            for username, state in _viewing_tracker._states.items():
                duration = int((now - state.first_seen).total_seconds())
                if duration < 60:
                    continue  # Not yet counted
                # Find or add user in user_totals
                found = False
                for u in stats.get("user_totals", []):
                    if u["username"] == username:
                        u["total"] += duration
                        found = True
                        break
                if not found:
                    stats.setdefault("user_totals", []).append({
                        "username": username,
                        "total": duration,
                        "sessions": 1,
                    })

        return stats
    finally:
        await db.close()


@app.get("/api/stats/user/{username}")
async def api_user_stats(username: str, days: int = 30):
    """Get viewing stats for a specific user, including active session."""
    db = await get_db()
    try:
        stats = await get_user_stats(db, username=username, days=days)

        # Include currently active session from viewing tracker
        if _viewing_tracker and username in _viewing_tracker._states:
            from datetime import datetime
            now = datetime.now()
            state = _viewing_tracker._states[username]
            duration = int((now - state.first_seen).total_seconds())
            if duration >= 60:
                stats["total_seconds"] = stats.get("total_seconds", 0) + duration
                stats["total_hours"] = round(stats["total_seconds"] / 3600, 1)
                # Add current channel to top_channels if not already there
                channel = state.channel_name or f"SID {state.sid}"
                found = False
                for c in stats.get("top_channels", []):
                    if c["channel_name"] == channel:
                        c["total"] += duration
                        c["sessions"] += 1
                        found = True
                        break
                if not found:
                    stats.setdefault("top_channels", []).append({
                        "channel_name": channel,
                        "country_tag": "unknown",
                        "total": duration,
                        "sessions": 1,
                    })

        return stats
    finally:
        await db.close()


@app.get("/api/stats/cards")
async def api_card_stats(days: int = 30):
    """Get per-card (reader) busy hours and daily stats."""
    db = await get_db()
    try:
        stats = await get_card_stats(db, days=days)
        return stats
    finally:
        await db.close()


@app.get("/api/mappings")
async def api_mappings():
    """Get all channel mappings."""
    db = await get_db()
    try:
        mappings = await get_channel_mappings(db)
        return {"mappings": mappings}
    finally:
        await db.close()


@app.delete("/api/mappings/clear")
async def api_clear_mappings():
    """Delete all channel mappings."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM channel_mappings")
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@app.delete("/api/ecm/clear")
async def api_clear_ecm():
    """Delete all ECM event data (resets services suggestions)."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM ecm_events")
        await db.execute("DELETE FROM watch_sessions")
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


@app.get("/api/mappings/export")
async def api_export_services(caids: str | None = None):
    """Export mappings as oscam.services format, grouped by exclusive reader.
    
    A SID is 'exclusive' to a reader if:
    - That reader successfully decoded it
    - Other readers of the same CAID NEVER successfully decoded it
    
    This avoids false positives from initial "not found" attempts
    before EMMs/ECMs warm up the card.
    """
    db = await get_db()
    try:
        # Parse CAID filter
        caid_filter = [c.strip().upper() for c in caids.split(",")] if caids else None

        # Get all successful decodes grouped by caid, sid, reader
        query = """
            SELECT caid, sid, reader
            FROM ecm_events
            WHERE success = 1 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
        """
        params = []
        if caid_filter:
            placeholders = ",".join("?" * len(caid_filter))
            query += f" AND caid IN ({placeholders})"
            params = caid_filter
        query += " GROUP BY caid, sid, reader"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        # Build: for each CAID:SID → set of readers that successfully decoded it
        sid_readers = {}  # (caid, sid) → set of readers
        for row in rows:
            key = (row["caid"], row["sid"])
            if key not in sid_readers:
                sid_readers[key] = set()
            sid_readers[key].add(row["reader"])

        # Get reader country tags from config
        cfg = get_config()
        reader_tags = cfg.readers  # e.g. {"Skylink-SK": "SK", "Skylink-CZ": "CZ"}

        # Find all readers per CAID (to know which readers could potentially decode)
        caid_readers = {}  # caid → set of all readers that decoded anything on this caid
        for (caid, sid), readers_set in sid_readers.items():
            if caid not in caid_readers:
                caid_readers[caid] = set()
            caid_readers[caid].update(readers_set)

        # Also get explicit failures
        fail_query = """
            SELECT caid, sid, reader
            FROM ecm_events
            WHERE success = 0 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
        """
        fail_params = []
        if caid_filter:
            placeholders = ",".join("?" * len(caid_filter))
            fail_query += f" AND caid IN ({placeholders})"
            fail_params = caid_filter
        fail_query += " GROUP BY caid, sid, reader"
        cursor_fail = await db.execute(fail_query, fail_params)
        fail_rows = await cursor_fail.fetchall()

        sid_fail = {}
        for row in fail_rows:
            key = (row["caid"], row["sid"])
            if key not in sid_fail:
                sid_fail[key] = set()
            sid_fail[key].add(row["reader"])

        # Group SIDs: exclusive means reader X succeeded AND another reader explicitly FAILED
        groups = {}  # group_name → {"caid": str, "sids": set}
        for (caid, sid), succeeded_readers in sid_readers.items():
            failed_readers = sid_fail.get((caid, sid), set())
            for reader in succeeded_readers:
                others_failed = failed_readers - {reader}
                others_succeeded = succeeded_readers - {reader}
                if others_failed and not others_succeeded:
                    group_key = f"{reader}|{caid}"
                    if group_key not in groups:
                        groups[group_key] = {"caid": caid, "reader": reader, "sids": set()}
                    groups[group_key]["sids"].add(sid)

        # Generate oscam.services format
        lines = [
            "# oscam.services generated by OScam Monitor",
            f"# Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "# Only includes SIDs exclusive to one reader (decoded by one, never by others)",
            "",
        ]
        for group_key, group in sorted(groups.items(), key=lambda x: x[0]):
            name = group["reader"].lower().replace(" ", "-")
            sids = ",".join(sorted(group["sids"]))
            lines.append(f"[{name}]")
            lines.append(f"caid                          = {group['caid']}")
            lines.append(f"provid                        = ")
            lines.append(f"srvid                         = {sids}")
            lines.append("")

        content = "\n".join(lines)
        return HTMLResponse(content=content, media_type="text/plain")
    finally:
        await db.close()


@app.get("/api/mappings/export-srvid2")
async def api_export_srvid2(caids: str | None = None):
    """Export channel mappings as oscam.srvid2 format.

    Format: SID:CAID[@PROVID]|Channel Name|||Provider
    Grouped by provider/CAID with header comments.
    """
    from .channel_list import get_provider_by_caid

    db = await get_db()
    try:
        caid_filter = [c.strip().upper() for c in caids.split(",")] if caids else None

        query = "SELECT caid, sid, channel_name FROM channel_mappings WHERE channel_name IS NOT NULL AND channel_name != ''"
        params = []
        if caid_filter:
            placeholders = ",".join("?" * len(caid_filter))
            query += f" AND caid IN ({placeholders})"
            params = caid_filter
        query += " ORDER BY caid, sid"

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        # Group by CAID
        by_caid = {}
        for row in rows:
            caid = row["caid"]
            if caid not in by_caid:
                by_caid[caid] = []
            by_caid[caid].append(row)

        lines = [
            "# oscam.srvid2 generated by OScam Monitor",
            f"# Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        for caid in sorted(by_caid.keys()):
            provider = get_provider_by_caid(caid) or caid
            lines.append(f"## PROVIDER: {provider}")
            lines.append(f"## CAID: {caid}")
            lines.append("")
            for row in sorted(by_caid[caid], key=lambda r: r["sid"]):
                sid = row["sid"]
                name = row["channel_name"]
                lines.append(f"{sid}:{caid}|{name}|||{provider}")
            lines.append("")

        content = "\n".join(lines)
        return HTMLResponse(content=content, media_type="text/plain")
    finally:
        await db.close()


# --- File Sync API ---

@app.get("/api/sync/srvid2/suggestions")
async def api_srvid2_suggestions(caids: str | None = None):
    """Fetch server srvid2, diff against local mappings, return suggestions."""
    from .file_sync import OscamFileClient, parse_srvid2, diff_srvid2
    from .channel_list import get_provider_by_caid

    cfg = get_config()
    if not cfg.server:
        raise HTTPException(status_code=400, detail="No server configured")

    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)
    content = await client.fetch_file("oscam.srvid2")
    if content is None:
        raise HTTPException(status_code=502, detail="Failed to fetch oscam.srvid2 from server")

    server_entries = parse_srvid2(content)

    # Get local mappings from DB
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT caid, sid, channel_name FROM channel_mappings WHERE channel_name IS NOT NULL AND channel_name != '' AND caid != '0000'"
        )
        local_mappings = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

    suggestions = diff_srvid2(server_entries, local_mappings)

    # Enrich with provider
    for s in suggestions:
        if not s.get("provider"):
            s["provider"] = get_provider_by_caid(s["caid"]) or ""

    # Filter by selected CAIDs
    if caids:
        caid_filter = [c.strip().upper() for c in caids.split(",")]
        suggestions = [s for s in suggestions if s["caid"] in caid_filter]

    return {
        "server_entries_count": len(server_entries),
        "local_mappings_count": len(local_mappings),
        "suggestions": suggestions,
    }


@app.get("/api/sync/services/suggestions")
async def api_services_suggestions():
    """Fetch server services, diff against identified exclusive SIDs."""
    from .file_sync import OscamFileClient, parse_services, diff_services

    cfg = get_config()
    if not cfg.server:
        raise HTTPException(status_code=400, detail="No server configured")

    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)
    content = await client.fetch_file("oscam.services")
    if content is None:
        raise HTTPException(status_code=502, detail="Failed to fetch oscam.services from server")

    server_groups = parse_services(content)

    # Get exclusive SIDs from ECM data (same logic as export)
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT caid, sid, reader
            FROM ecm_events
            WHERE success = 1 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
            GROUP BY caid, sid, reader
        """)
        rows = await cursor.fetchall()

        # Get channel names for enrichment
        cursor2 = await db.execute(
            "SELECT caid, sid, channel_name FROM channel_mappings WHERE channel_name IS NOT NULL"
        )
        name_map = {(r["caid"], r["sid"]): r["channel_name"] for r in await cursor2.fetchall()}
    finally:
        await db.close()

    # Build exclusive SIDs: reader X succeeded AND another reader explicitly failed
    # A SID is exclusive to reader X only if:
    # - Reader X decoded it successfully
    # - Another reader of the same CAID FAILED on it (not found / rejected)
    sid_success = {}  # (caid, sid) → set of readers that succeeded
    sid_fail = {}     # (caid, sid) → set of readers that failed
    for row in rows:
        key = (row["caid"], row["sid"])
        if key not in sid_success:
            sid_success[key] = set()
        sid_success[key].add(row["reader"])

    # Also get failures
    db = await get_db()
    try:
        cursor_fail = await db.execute("""
            SELECT caid, sid, reader
            FROM ecm_events
            WHERE success = 0 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
            GROUP BY caid, sid, reader
        """)
        for row in await cursor_fail.fetchall():
            key = (row["caid"], row["sid"])
            if key not in sid_fail:
                sid_fail[key] = set()
            sid_fail[key].add(row["reader"])
    finally:
        await db.close()

    exclusive_sids = {}
    for (caid, sid), succeeded_readers in sid_success.items():
        failed_readers = sid_fail.get((caid, sid), set())
        for reader in succeeded_readers:
            # Check if another reader explicitly failed on this SID
            others_failed = failed_readers - {reader}
            others_succeeded = succeeded_readers - {reader}
            if others_failed and not others_succeeded:
                # This reader succeeded, others failed, nobody else succeeded
                group_name = reader.lower().replace(" ", "-")
                if group_name not in exclusive_sids:
                    exclusive_sids[group_name] = set()
                exclusive_sids[group_name].add(sid)

    suggestions = diff_services(server_groups, exclusive_sids)

    # Enrich suggestions with channel names
    for s in suggestions:
        sid = s["sid"]
        # Try to find caid from the group
        group = next((g for g in server_groups if g.name == s["group"]), None)
        caid = group.caid if group else ""
        s["channel_name"] = name_map.get((caid, sid), "")
        s["caid"] = caid

    return {
        "server_groups": [{"name": g.name, "caid": g.caid, "srvid_count": len(g.srvids)} for g in server_groups],
        "suggestions": suggestions,
    }


@app.post("/api/sync/srvid2/push")
async def api_push_srvid2(data: dict):
    """Apply selected srvid2 suggestions and push to server. Backs up first."""
    from .file_sync import OscamFileClient, parse_srvid2, build_srvid2_content, Srvid2Entry
    from .channel_list import get_provider_by_caid

    cfg = get_config()
    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)

    # Fetch current file
    content = await client.fetch_file("oscam.srvid2")
    if content is None:
        raise HTTPException(status_code=502, detail="Failed to fetch file from server")

    # Backup current version
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO file_backups (filename, content, note) VALUES (?, ?, ?)",
            ("oscam.srvid2", content, "Auto-backup before push"),
        )
        await db.commit()
    finally:
        await db.close()

    # Parse existing and apply changes
    entries = parse_srvid2(content)
    entry_map = {(e.caid, e.sid): e for e in entries}

    # Apply selected suggestions
    selected = data.get("selected", [])
    for s in selected:
        caid = s["caid"].upper()
        sid = s["sid"].upper()
        name = s["channel_name"]
        provider = s.get("provider") or get_provider_by_caid(caid) or ""
        key = (caid, sid)

        if key in entry_map:
            entry_map[key].channel_name = name
        else:
            entries.append(Srvid2Entry(
                sid=sid, caid=caid, channel_name=name, provider=provider,
            ))

    # Rebuild and push
    new_content = build_srvid2_content(entries)
    success = await client.push_file("oscam.srvid2", new_content)

    return {"status": "ok" if success else "failed", "entries_count": len(entries)}


@app.post("/api/sync/services/push")
async def api_push_services(data: dict):
    """Apply selected services suggestions and push to server. Backs up first."""
    from .file_sync import OscamFileClient, parse_services, build_services_content

    cfg = get_config()
    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)

    # Fetch current file
    content = await client.fetch_file("oscam.services")
    if content is None:
        raise HTTPException(status_code=502, detail="Failed to fetch file from server")

    # Backup
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO file_backups (filename, content, note) VALUES (?, ?, ?)",
            ("oscam.services", content, "Auto-backup before push"),
        )
        await db.commit()
    finally:
        await db.close()

    # Parse and apply
    groups = parse_services(content)
    group_map = {g.name: g for g in groups}

    selected = data.get("selected", [])
    for s in selected:
        group_name = s["group"]
        sid = s["sid"].upper()
        if group_name in group_map:
            if sid not in group_map[group_name].srvids:
                group_map[group_name].srvids.append(sid)

    # Rebuild and push
    new_content = build_services_content(groups)
    success = await client.push_file("oscam.services", new_content)

    return {"status": "ok" if success else "failed"}


@app.get("/api/sync/backups")
async def api_list_backups(filename: str | None = None):
    """List file backups."""
    db = await get_db()
    try:
        if filename:
            cursor = await db.execute(
                "SELECT id, filename, created_at, note, LENGTH(content) as size FROM file_backups WHERE filename = ? ORDER BY created_at DESC",
                (filename,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, filename, created_at, note, LENGTH(content) as size FROM file_backups ORDER BY created_at DESC"
            )
        return {"backups": [dict(row) for row in await cursor.fetchall()]}
    finally:
        await db.close()


@app.post("/api/sync/backup/{filename}")
async def api_manual_backup(filename: str):
    """Manually create a backup of a file from the server."""
    from .file_sync import OscamFileClient

    if filename not in ("oscam.srvid2", "oscam.services"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    cfg = get_config()
    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)

    content = await client.fetch_file(filename)
    if content is None:
        raise HTTPException(status_code=502, detail=f"Failed to fetch {filename} from server")

    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO file_backups (filename, content, note) VALUES (?, ?, ?)",
            (filename, content, "Manual backup"),
        )
        await db.commit()
    finally:
        await db.close()

    return {"status": "ok", "filename": filename}


@app.post("/api/sync/restore/{backup_id}")
async def api_restore_backup(backup_id: int):
    """Restore a backup — pushes it back to the server (backs up current first)."""
    from .file_sync import OscamFileClient

    db = await get_db()
    try:
        cursor = await db.execute("SELECT filename, content FROM file_backups WHERE id = ?", (backup_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Backup not found")

        filename = row["filename"]
        restore_content = row["content"]
    finally:
        await db.close()

    cfg = get_config()
    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)

    # Backup current before restoring
    current = await client.fetch_file(filename)
    if current:
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO file_backups (filename, content, note) VALUES (?, ?, ?)",
                (filename, current, "Auto-backup before restore"),
            )
            await db.commit()
        finally:
            await db.close()

    # Push the restore
    success = await client.push_file(filename, restore_content)
    return {"status": "ok" if success else "failed", "filename": filename}


@app.get("/api/sync/suggestion-counts")
async def api_suggestion_counts():
    """Quick count of pending suggestions for badge display (without full diff)."""
    from .file_sync import OscamFileClient, parse_srvid2, diff_srvid2

    cfg = get_config()
    if not cfg.server:
        return {"srvid2": 0, "services": 0}

    srv = cfg.server
    client = OscamFileClient(srv.host, srv.port, srv.username, srv.password)

    srvid2_count = 0
    try:
        content = await client.fetch_file("oscam.srvid2")
        if content:
            server_entries = parse_srvid2(content)
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT caid, sid, channel_name FROM channel_mappings WHERE channel_name IS NOT NULL AND channel_name != '' AND caid != '0000'"
                )
                local_mappings = [dict(row) for row in await cursor.fetchall()]
            finally:
                await db.close()
            srvid2_count = len(diff_srvid2(server_entries, local_mappings))
    except Exception:
        pass

    services_count = 0
    try:
        from .file_sync import parse_services, diff_services
        content = await client.fetch_file("oscam.services")
        if content:
            server_groups = parse_services(content)
            db = await get_db()
            try:
                cursor = await db.execute("""
                    SELECT caid, sid, reader
                    FROM ecm_events
                    WHERE success = 1 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
                    GROUP BY caid, sid, reader
                """)
                rows = await cursor.fetchall()
            finally:
                await db.close()

            sid_readers = {}
            for row in rows:
                key = (row["caid"], row["sid"])
                if key not in sid_readers:
                    sid_readers[key] = set()
                sid_readers[key].add(row["reader"])

            # Get failures
            db = await get_db()
            try:
                cursor_f = await db.execute("""
                    SELECT caid, sid, reader
                    FROM ecm_events
                    WHERE success = 0 AND reader IS NOT NULL AND reader != '' AND sid != '0000'
                    GROUP BY caid, sid, reader
                """)
                fail_rows = await cursor_f.fetchall()
            finally:
                await db.close()

            sid_fail = {}
            for row in fail_rows:
                key = (row["caid"], row["sid"])
                if key not in sid_fail:
                    sid_fail[key] = set()
                sid_fail[key].add(row["reader"])

            exclusive_sids = {}
            for (caid, sid), succeeded_readers in sid_readers.items():
                failed_readers = sid_fail.get((caid, sid), set())
                for reader in succeeded_readers:
                    others_failed = failed_readers - {reader}
                    others_succeeded = succeeded_readers - {reader}
                    if others_failed and not others_succeeded:
                        group_name = reader.lower().replace(" ", "-")
                        if group_name not in exclusive_sids:
                            exclusive_sids[group_name] = set()
                        exclusive_sids[group_name].add(sid)

            services_count = len(diff_services(server_groups, exclusive_sids))
    except Exception:
        pass

    return {"srvid2": srvid2_count, "services": services_count}


# --- Automapping API ---

@app.get("/api/automap/status")
async def api_automap_status():
    """Get current automapping status."""
    from .automapping import get_automap_status
    return get_automap_status()


@app.post("/api/automap/start")
async def api_automap_start(data: dict):
    """Start automapping on a receiver."""
    from .automapping import run_automap, _status
    from .database import upsert_channel_mapping

    if _status.running:
        raise HTTPException(status_code=409, detail="Automapping already running")

    username = data.get("username")
    interval = data.get("interval", 10)

    cfg = get_config()
    device = cfg.user_device_map.get(username)
    if not device:
        raise HTTPException(status_code=400, detail=f"No receiver mapped for user '{username}'")

    async def store_mapping(caid: str, sid: str, channel_name: str):
        # We don't know the CAID from the receiver — get it from OScam status
        # For now store without CAID, will be enriched by OScam polling
        db = await get_db()
        try:
            # Try to find CAID from OScam for this SID
            cursor = await db.execute(
                "SELECT caid FROM ecm_events WHERE sid = ? AND success = 1 ORDER BY timestamp DESC LIMIT 1",
                (sid,),
            )
            row = await cursor.fetchone()
            actual_caid = row["caid"] if row else caid or "0000"

            from .database import upsert_channel_mapping
            await upsert_channel_mapping(db, caid=actual_caid, sid=sid, channel_name=channel_name)
        finally:
            await db.close()

    async def check_ecm(sid: str) -> bool:
        """Check if we have any ECM events for this SID (meaning OScam decoded it)."""
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM ecm_events WHERE sid = ? AND success = 1",
                (sid,),
            )
            row = await cursor.fetchone()
            return row["cnt"] > 0 if row else False
        finally:
            await db.close()

    async def check_oscam_decoding(user: str, sid: str) -> bool:
        """Check if OScam is actively decoding this SID for this user right now."""
        for srv_name, client in _api_clients.items():
            try:
                status = await client.get_status()
                for c in status.get("clients", []):
                    if c.username == user and c.sid and c.sid.upper() == sid.upper():
                        return True
            except Exception:
                pass
        return False

    import asyncio
    asyncio.create_task(run_automap(
        receiver_ip=device.ip,
        receiver_port=device.port,
        username=device.username,
        password=device.password,
        interval=interval,
        db_callback=store_mapping,
        ecm_check=check_ecm,
        display_name=username,
        oscam_check=check_oscam_decoding,
    ))

    return {"status": "started", "username": username}


@app.post("/api/automap/stop")
async def api_automap_stop():
    """Stop automapping."""
    from .automapping import stop_automap
    stop_automap()
    return {"status": "stopping"}


@app.get("/api/serials")
async def api_serials():
    """Get known card serials."""
    if _serial_monitor:
        return {"serials": _serial_monitor.get_known_serials()}
    return {"serials": {}}


@app.get("/api/readers")
async def api_readers(server: str | None = None):
    """Get reader info with entitlement counts from OScam API."""
    results = {}
    targets = {server: _api_clients[server]} if server and server in _api_clients else _api_clients

    for srv_name, client in targets.items():
        try:
            readers = await client.get_reader_info_with_entitlements()
            results[srv_name] = [
                {
                    "label": r.label,
                    "protocol": r.protocol,
                    "caid": r.caid,
                    "entitlements_count": r.entitlements_count,
                    "card_serial": r.card_serial,
                    "status": r.status,
                }
                for r in readers
            ]
        except Exception as e:
            results[srv_name] = {"error": str(e)}

    return {"readers": results}


@app.get("/api/readers/{server_name}/{reader_label}/entitlements")
async def api_reader_entitlements(server_name: str, reader_label: str):
    """Get entitlements for a specific reader."""
    if server_name not in _api_clients:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    client = _api_clients[server_name]
    entitlements = await client.get_entitlements(reader_label)
    return {
        "reader": reader_label,
        "server": server_name,
        "entitlements": [
            {"caid": e.caid, "provid": e.provid, "exp_date": e.exp_date}
            for e in entitlements
        ],
        "count": len(entitlements),
    }


@app.get("/api/server/{server_name}/status")
async def api_server_status(server_name: str):
    """Get live status from a specific OScam server."""
    if server_name not in _api_clients:
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")

    client = _api_clients[server_name]
    status = await client.get_status()
    return {
        "server": server_name,
        "connected": True,
        "clients": [
            {
                "username": c.username,
                "ip": c.ip,
                "protocol": c.protocol,
                "caid": c.caid,
                "sid": c.sid,
                "ecm_time_ms": c.last_ecm_time,
                "reader": c.reader,
                "idle_seconds": c.idle_seconds,
            }
            for c in status.get("clients", [])
        ],
    }


@app.get("/api/users/discovered")
async def api_discovered_users(server: str | None = None):
    """Get discovered users from log parsing."""
    db = await get_db()
    try:
        users = await get_discovered_users(db, server=server)
        return {"users": users}
    finally:
        await db.close()


@app.delete("/api/users/discovered/{username}")
async def api_delete_discovered_user(username: str):
    """Delete a discovered user and all their stats."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM discovered_users WHERE username = ?", (username,))
        await db.execute("DELETE FROM watch_sessions WHERE username = ?", (username,))
        await db.execute("DELETE FROM ecm_events WHERE username = ?", (username,))
        await db.commit()
        return {"status": "ok"}
    finally:
        await db.close()


# --- Settings API ---


@app.get("/api/settings")
async def api_get_settings():
    """Get current configuration."""
    cfg = get_config()
    return cfg.model_dump()


@app.get("/api/settings/server")
async def api_get_server():
    """Get the configured server."""
    cfg = get_config()
    return {"server": cfg.server.model_dump() if cfg.server else None}


@app.put("/api/settings/timezone")
async def api_set_timezone(data: dict):
    """Set the display timezone."""
    from .config import save_config
    tz = data.get("timezone", "Europe/Bratislava")
    cfg = get_config()
    cfg.web.timezone = tz
    save_config(cfg)
    return {"status": "ok", "timezone": tz}


@app.put("/api/settings/auth")
async def api_set_auth(data: dict):
    """Update dashboard login credentials."""
    from .config import save_config
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    cfg = get_config()
    cfg.web.username = username
    cfg.web.password = password
    save_config(cfg)
    return {"status": "ok"}


@app.post("/api/settings/server")
async def api_set_server_endpoint(server: ServerCreate):
    """Set the OScam server configuration."""
    cfg = set_server(OscamServer(**server.model_dump()))
    return {"status": "ok", "server": cfg.server.model_dump() if cfg.server else None}


@app.put("/api/settings/server")
async def api_update_server_endpoint(updates: ServerUpdate):
    """Update the server settings."""
    try:
        update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
        cfg = update_server(update_data)
        return {"status": "ok", "server": cfg.server.model_dump() if cfg.server else None}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/settings/server")
async def api_remove_server_endpoint():
    """Remove the server configuration."""
    cfg = remove_server()
    return {"status": "ok", "server": None}


@app.get("/api/settings/user-devices")
async def api_get_user_devices():
    """Get user-device mappings."""
    cfg = get_config()
    return {"user_device_map": cfg.user_device_map}


@app.post("/api/settings/user-devices")
async def api_set_user_device(mapping: UserDeviceMapping):
    """Set a user → device mapping."""
    device = ReceiverDevice(
        ip=mapping.receiver_ip,
        port=mapping.receiver_port,
        username=mapping.receiver_username,
        password=mapping.receiver_password,
    )
    cfg = set_user_device(mapping.username, device)
    return {"status": "ok", "user_device_map": cfg.user_device_map}


@app.delete("/api/settings/user-devices/{username}")
async def api_remove_user_device(username: str):
    """Remove a user-device mapping."""
    cfg = remove_user_device(username)
    return {"status": "ok", "user_device_map": cfg.user_device_map}


@app.get("/api/settings/readers")
async def api_get_readers():
    """Get reader country tags."""
    cfg = get_config()
    return {"readers": cfg.readers}


@app.post("/api/settings/readers")
async def api_set_reader(reader: ReaderCountry):
    """Tag a reader with a country."""
    if reader.country.upper() not in ("SK", "CZ"):
        raise HTTPException(status_code=400, detail="Country must be SK or CZ")
    cfg = set_reader_country(reader.reader_name, reader.country)
    return {"status": "ok", "readers": cfg.readers}


@app.delete("/api/settings/readers/{reader_name}")
async def api_remove_reader(reader_name: str):
    """Remove a reader country tag."""
    cfg = remove_reader_country(reader_name)
    return {"status": "ok", "readers": cfg.readers}


# --- Frontend serving ---

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main dashboard page."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text())
    return HTMLResponse(content="<h1>OScam Monitor</h1><p>Frontend not built yet.</p>")


# Mount static files if directory exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
