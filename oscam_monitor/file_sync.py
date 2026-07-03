"""File sync - fetch/push oscam.srvid2 and oscam.services from/to OScam server via WebIF."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Srvid2Entry:
    """Single entry from oscam.srvid2."""
    sid: str
    caid: str
    provid: str = ""
    channel_name: str = ""
    provider: str = ""


@dataclass
class ServiceGroup:
    """A [group] section from oscam.services."""
    name: str
    caid: str = ""
    provid: str = ""
    srvids: list[str] = field(default_factory=list)


class OscamFileClient:
    """Reads and writes OScam config files via WebIF."""

    def __init__(self, host: str, port: int, username: str | None = None, password: str | None = None):
        self.base_url = f"http://{host}:{port}"
        self.auth = httpx.DigestAuth(username, password) if username and password else None
        self.timeout = 15.0

    async def fetch_file(self, filename: str) -> str | None:
        """Fetch a config file's content from OScam WebIF."""
        url = f"{self.base_url}/files.html"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {"params": {"file": filename}}
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.get(url, **kwargs)
                if resp.status_code != 200:
                    logger.warning(f"Failed to fetch {filename}: HTTP {resp.status_code}")
                    return None

                # Extract content from <textarea name="filecontent">...</textarea>
                match = re.search(
                    r'<textarea[^>]*name="filecontent"[^>]*>(.*?)</textarea>',
                    resp.text,
                    re.DOTALL,
                )
                if match:
                    content = match.group(1)
                    # Unescape HTML entities
                    content = content.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                    return content
                logger.warning(f"No textarea found in response for {filename}")
                return None
        except Exception as e:
            logger.debug(f"Error fetching {filename} from OScam: {e}")
            return None

    async def push_file(self, filename: str, content: str) -> bool:
        """Push updated file content to OScam WebIF."""
        url = f"{self.base_url}/files.html"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {
                    "data": {
                        "file": filename,
                        "filecontent": content,
                        "action": "Save",
                    }
                }
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.post(url, **kwargs)
                if resp.status_code == 200:
                    logger.info(f"Successfully pushed {filename} to OScam")
                    return True
                else:
                    logger.warning(f"Failed to push {filename}: HTTP {resp.status_code}")
                    return False
        except Exception as e:
            logger.error(f"Error pushing {filename} to OScam: {e}")
            return False


def parse_srvid2(content: str) -> list[Srvid2Entry]:
    """Parse oscam.srvid2 file content into structured entries."""
    entries = []
    current_provider = ""
    current_caid = ""

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            # Check for ## PROVIDER: and ## CAID: headers
            if line.startswith("## PROVIDER:"):
                current_provider = line.split(":", 1)[1].strip()
            elif line.startswith("## CAID:"):
                current_caid = line.split(":", 1)[1].strip()
            continue

        # Format: SID:CAID[@PROVID]|Channel Name|||Provider
        parts = line.split("|")
        if len(parts) < 2:
            continue

        id_part = parts[0].strip()
        channel_name = parts[1].strip()
        provider = parts[-1].strip() if len(parts) >= 4 else current_provider

        # Parse SID:CAID[@PROVID]
        provid = ""
        if "@" in id_part:
            id_part_base, provid = id_part.split("@", 1)
        else:
            id_part_base = id_part

        if ":" not in id_part_base:
            continue

        sid, caid = id_part_base.split(":", 1)

        entries.append(Srvid2Entry(
            sid=sid.upper(),
            caid=caid.upper(),
            provid=provid,
            channel_name=channel_name,
            provider=provider or current_provider,
        ))

    return entries


def parse_services(content: str) -> list[ServiceGroup]:
    """Parse oscam.services file content into groups."""
    groups = []
    current = None

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # New group
        if line.startswith("[") and line.endswith("]"):
            if current:
                groups.append(current)
            current = ServiceGroup(name=line[1:-1])
            continue

        if current is None:
            continue

        # Parse key = value
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()

            if key == "caid":
                current.caid = val
            elif key == "provid":
                current.provid = val
            elif key == "srvid":
                current.srvids = [s.strip().upper() for s in val.split(",") if s.strip()]

    if current:
        groups.append(current)

    return groups


def build_srvid2_content(entries: list[Srvid2Entry]) -> str:
    """Build oscam.srvid2 file content from entries."""
    # Group by provider/caid
    by_provider: dict[str, dict[str, list[Srvid2Entry]]] = {}
    for e in entries:
        prov = e.provider or "Unknown"
        if prov not in by_provider:
            by_provider[prov] = {}
        if e.caid not in by_provider[prov]:
            by_provider[prov][e.caid] = []
        by_provider[prov][e.caid].append(e)

    lines = [
        "# oscam.srvid2 generated by OScam Monitor",
        "",
    ]

    for provider in sorted(by_provider.keys()):
        for caid in sorted(by_provider[provider].keys()):
            entries_list = sorted(by_provider[provider][caid], key=lambda e: e.sid)
            lines.append(f"## PROVIDER: {provider}")
            lines.append(f"## CAID: {caid}")
            lines.append("")
            for e in entries_list:
                provid_part = f"@{e.provid}" if e.provid else ""
                lines.append(f"{e.sid}:{e.caid}{provid_part}|{e.channel_name}|||{e.provider}")
            lines.append("")

    return "\n".join(lines)


def build_services_content(groups: list[ServiceGroup]) -> str:
    """Build oscam.services file content from groups."""
    lines = [
        "# oscam.services generated by OScam Monitor",
        "",
    ]
    for g in groups:
        lines.append(f"[{g.name}]")
        if g.caid:
            lines.append(f"caid                          = {g.caid}")
        if g.provid:
            lines.append(f"provid                        = {g.provid}")
        if g.srvids:
            lines.append(f"srvid                         = {','.join(g.srvids)}")
        lines.append("")

    return "\n".join(lines)


def diff_srvid2(server_entries: list[Srvid2Entry], local_mappings: list[dict]) -> list[dict]:
    """
    Compare server srvid2 entries with local DB mappings.
    Returns list of suggested updates (new or changed names).
    """
    # Build lookup from server file: (caid, sid) → entry
    server_map = {}
    for e in server_entries:
        key = (e.caid, e.sid)
        server_map[key] = e

    suggestions = []
    for m in local_mappings:
        caid = m["caid"].upper()
        sid = m["sid"].upper()
        local_name = m.get("channel_name") or ""
        if not local_name:
            continue

        key = (caid, sid)
        server_entry = server_map.get(key)

        if server_entry is None:
            # New SID not in server file
            suggestions.append({
                "caid": caid,
                "sid": sid,
                "channel_name": local_name,
                "current_name": None,
                "action": "add",
                "provider": m.get("provider", ""),
            })
        elif server_entry.channel_name != local_name:
            # Name differs
            suggestions.append({
                "caid": caid,
                "sid": sid,
                "channel_name": local_name,
                "current_name": server_entry.channel_name,
                "action": "update",
                "provider": server_entry.provider,
            })

    return suggestions


def diff_services(server_groups: list[ServiceGroup], exclusive_sids: dict[str, set]) -> list[dict]:
    """
    Compare server services groups with locally identified exclusive SIDs.
    exclusive_sids: {group_name: set of SIDs that should be in that group}
    Returns list of suggested additions per group.
    """
    suggestions = []

    for group_name, sids in exclusive_sids.items():
        # Find matching server group
        server_group = next((g for g in server_groups if g.name == group_name), None)
        existing_sids = set(s.upper() for s in server_group.srvids) if server_group else set()

        new_sids = sids - existing_sids
        for sid in sorted(new_sids):
            suggestions.append({
                "group": group_name,
                "sid": sid,
                "action": "add",
            })

    return suggestions
