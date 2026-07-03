"""Automapping - cycles through channels on a receiver to build SID→name mappings."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AutomapStatus:
    """Current status of an automapping run."""
    running: bool = False
    username: str = ""
    channels_found: int = 0
    current_channel: str = ""
    first_channel: str = ""
    started_at: str = ""
    log: list[str] = field(default_factory=list)


_status = AutomapStatus()
_stop_flag = False


def get_automap_status() -> dict:
    return {
        "running": _status.running,
        "username": _status.username,
        "channels_found": _status.channels_found,
        "current_channel": _status.current_channel,
        "first_channel": _status.first_channel,
        "started_at": _status.started_at,
        "log": _status.log[-20:],  # Last 20 entries
    }


def stop_automap():
    global _stop_flag
    _stop_flag = True


async def run_automap(receiver_ip: str, receiver_port: int, username: str,
                      password: str | None, interval: int, db_callback, ecm_check=None,
                      display_name: str = "", oscam_check=None) -> None:
    """
    Run automapping: cycle through channels on receiver, record SID→name.

    Steps:
    1. Get current channel (first channel in bouquet)
    2. Record it
    3. Send channel-up
    4. Wait interval seconds
    5. Record new channel
    6. Repeat until we see the first channel again
    """
    global _stop_flag, _status

    _stop_flag = False
    _status = AutomapStatus(
        running=True,
        username=display_name or receiver_ip,
        started_at=datetime.now().strftime("%H:%M:%S"),
    )

    base_url = f"http://{receiver_ip}:{receiver_port}"
    auth = httpx.BasicAuth(username, password) if username and password else None

    try:
        # Get first channel
        info = await _get_current(base_url, auth)
        if not info:
            _status.log.append("ERROR: Cannot connect to receiver")
            _status.running = False
            return

        _status.first_channel = info["name"]
        _status.current_channel = info["name"]
        _status.log.append(f"Started on: {info['name']} (SID {info['sid']})")

        # Record first channel
        if info["sid"]:
            await db_callback(info["caid"], info["sid"], info["name"])
            _status.channels_found += 1

        # Cycle
        while not _stop_flag:
            # Send channel up (remote key 106)
            await _send_key(base_url, auth, 106)
            await asyncio.sleep(interval)

            if _stop_flag:
                break

            # Get new channel
            info = await _get_current(base_url, auth)
            if not info:
                _status.log.append("WARNING: Lost connection, retrying...")
                await asyncio.sleep(3)
                continue

            _status.current_channel = info["name"]

            # Check if we looped
            if info["name"] == _status.first_channel:
                _status.log.append(f"✓ Completed full loop! {_status.channels_found} channels mapped.")
                break

            # Record
            if info["sid"]:
                await db_callback(info["caid"], info["sid"], info["name"])
                _status.channels_found += 1
                # Check if OScam is actively decoding this SID for this user RIGHT NOW
                is_decoding = False
                if oscam_check:
                    is_decoding = await oscam_check(display_name, info["sid"])

                if is_decoding:
                    _status.log.append(f"[OK] {info['sid']} → {info['name']}")
                else:
                    # No active decode - could be FTA or dead channel
                    # Check if we ever had ECM for this SID historically
                    has_ecm = await ecm_check(info["sid"]) if ecm_check else False
                    if has_ecm:
                        # Was decoded before, just not right now (maybe card warming up)
                        _status.log.append(f"[OK] {info['sid']} → {info['name']}")
                    else:
                        _status.log.append(f"[FTA/ERR] {info['sid']} → {info['name']}")

    except Exception as e:
        _status.log.append(f"ERROR: {e}")
    finally:
        _status.running = False
        _status.log.append(f"Stopped. Total: {_status.channels_found} channels.")


async def _get_current(base_url: str, auth) -> dict | None:
    """Get current channel info from receiver."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            kwargs = {}
            if auth:
                kwargs["auth"] = auth

            # Get channel name and service ref from statusinfo
            resp = await client.get(f"{base_url}/api/statusinfo", **kwargs)
            if resp.status_code != 200:
                return None
            data = resp.json()
            name = data.get("currservice_station", "") or data.get("currservice_name", "")
            sref = data.get("currservice_serviceref", "")

            # Extract SID from service ref
            sid = ""
            parts = sref.split(":")
            if len(parts) >= 4:
                try:
                    sid = f"{int(parts[3], 16):04X}"
                except ValueError:
                    pass

            # Check if video is actually playing via /web/getcurrent (vpid > 0 = tuned)
            is_playing = False
            try:
                resp2 = await client.get(f"{base_url}/web/getcurrent", **kwargs)
                if resp2.status_code == 200:
                    import re
                    vpid_match = re.search(r"<e2vpid>(\d+)</e2vpid>", resp2.text)
                    if vpid_match and int(vpid_match.group(1)) > 0:
                        is_playing = True
            except Exception:
                pass

            return {"name": name, "sid": sid, "caid": "", "sref": sref, "is_playing": is_playing}
    except Exception:
        return None


async def _send_key(base_url: str, auth, key: int) -> bool:
    """Send remote control key to receiver."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            kwargs = {"params": {"command": str(key)}}
            if auth:
                kwargs["auth"] = auth
            resp = await client.get(f"{base_url}/api/remotecontrol", **kwargs)
            return resp.status_code == 200
    except Exception:
        return False
