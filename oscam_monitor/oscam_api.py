"""OScam WebIF API client - connects to OScam server to fetch status, readers, entitlements, and logs."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ReaderInfo:
    """Reader information from OScam."""

    label: str
    protocol: str
    caid: list[str] = field(default_factory=list)
    entitlements_count: int = 0
    card_serial: str = ""
    status: str = ""  # "CONNECTED", "CARDOK", etc.
    group: str = ""
    description: str = ""


@dataclass
class EntitlementInfo:
    """Single entitlement entry for a reader."""

    caid: str
    provid: str
    exp_date: str = ""
    active: bool = True


@dataclass
class ClientInfo:
    """Active client/user from OScam status."""

    username: str
    ip: str = ""
    protocol: str = ""
    caid: str = ""
    sid: str = ""
    last_ecm_time: int = 0  # ms
    reader: str = ""
    status: str = ""
    idle_seconds: int = 0
    channel_name: str = ""  # from request element text content


class OscamApiClient:
    """Client for OScam WebIF API (XML-based)."""

    def __init__(self, host: str, port: int = 8888, username: str | None = None, password: str | None = None):
        self.base_url = f"http://{host}:{port}"
        self.auth = httpx.DigestAuth(username, password) if username and password else None
        self.timeout = 10.0

    async def _get_xml(self, part: str, **params) -> ET.Element | None:
        """Fetch an API endpoint and parse XML response."""
        url = f"{self.base_url}/oscamapi.html"
        params["part"] = part
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {"params": params}
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.get(url, **kwargs)
                if resp.status_code == 200:
                    return ET.fromstring(resp.text)
                else:
                    logger.warning(f"OScam API returned {resp.status_code} for part={part}")
                    return None
        except httpx.TimeoutException:
            logger.debug(f"Timeout connecting to OScam at {self.base_url}")
            return None
        except ET.ParseError as e:
            logger.error(f"XML parse error from OScam API: {e}")
            return None
        except Exception as e:
            logger.error(f"Error connecting to OScam at {self.base_url}: {e}")
            return None

    async def get_status(self) -> dict:
        """Get OScam server status including active clients."""
        root = await self._get_xml("status")
        if root is None:
            return {"clients": [], "readers": []}

        clients = []
        readers = []

        # Parse client entries
        for client_el in root.findall(".//client"):
            client_type = client_el.get("type", "")
            if client_type in ("r", "p"):
                # Reader/proxy
                connection_el = client_el.find("connection")
                reader_info = {
                    "label": client_el.get("name", ""),
                    "protocol": client_el.get("protocol", ""),
                    "status": (connection_el.text or "").strip() if connection_el is not None else "",
                }
                readers.append(reader_info)
            elif client_type in ("c",):
                # Client/user
                request = client_el.find("request")
                connection_el = client_el.find("connection")
                # Request text contains channel name, e.g. "Joj Sport [AntikSAT]"
                # Strip the [Provider] suffix since we show provider separately
                req_text = (request.text or "").strip() if request is not None else ""
                if req_text and "[" in req_text:
                    req_text = req_text[:req_text.rfind("[")].strip()
                clients.append(ClientInfo(
                    username=client_el.get("name", ""),
                    ip=connection_el.get("ip", "") if connection_el is not None else "",
                    protocol=client_el.get("protocol", ""),
                    caid=request.get("caid", "") if request is not None else "",
                    sid=request.get("srvid", "") if request is not None else "",
                    last_ecm_time=int(request.get("ecmtime", "0") or "0") if request is not None else 0,
                    reader=request.get("answered", "") if request is not None else "",
                    status=(connection_el.text or "").strip() if connection_el is not None else "",
                    idle_seconds=int((client_el.find("times").get("idle", "0") or "0")) if client_el.find("times") is not None else 0,
                    channel_name=req_text,
                ))

        return {"clients": clients, "readers": readers}

    async def get_readers(self) -> list[ReaderInfo]:
        """Get all configured readers from the status endpoint."""
        root = await self._get_xml("status")
        if root is None:
            return []

        readers = []
        for client_el in root.findall(".//client"):
            if client_el.get("type") not in ("r", "p"):
                continue

            label = client_el.get("name", "")
            protocol = client_el.get("protocol", "")
            desc = client_el.get("desc", "")
            connection_el = client_el.find("connection")
            status = (connection_el.text or "").strip() if connection_el is not None else ""

            # Get CAID from the request element if actively decoding
            request = client_el.find("request")
            caids = []
            if request is not None:
                caid = request.get("caid", "")
                if caid and caid != "0000":
                    caids.append(caid)

            readers.append(ReaderInfo(
                label=label,
                protocol=protocol,
                caid=caids,
                card_serial="",
                status=status,
                description=desc,
            ))

        return readers

    async def get_entitlements(self, reader_label: str) -> list[EntitlementInfo]:
        """Get entitlements for a specific reader.

        Falls back to scraping the HTML page since the XML API
        doesn't return entitlements for pcsc readers.
        """
        # Try XML API first
        root = await self._get_xml("entitlement", label=reader_label)
        if root is not None:
            entitlements = []
            for ent_el in root.findall(".//entitlement"):
                caid = ent_el.findtext("caid", "") or ent_el.get("caid", "")
                provid = ent_el.findtext("provid", "") or ent_el.get("provid", "")
                exp_date = ent_el.findtext("exp", "") or ent_el.findtext("expdate", "")
                entitlements.append(EntitlementInfo(
                    caid=caid.strip(),
                    provid=provid.strip(),
                    exp_date=exp_date.strip(),
                ))
            if entitlements:
                return entitlements

        # Fallback: scrape HTML entitlements page (needed for pcsc readers)
        return await self._get_entitlements_html(reader_label)

    async def _get_entitlements_html(self, reader_label: str) -> list[EntitlementInfo]:
        """Scrape entitlements from the HTML page."""
        import re
        url = f"{self.base_url}/entitlements.html"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {"params": {"label": reader_label}}
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.get(url, **kwargs)
                if resp.status_code != 200:
                    return []

            entitlements = []
            # Parse rows: <TR CLASS="e_valid|e_expired"><TD>type</TD><TD>CAID</TD><TD>PROVID</TD>...<TD>ExpDate</TD>...
            for match in re.finditer(
                r'<TR CLASS="(e_valid|e_expired)">'
                r'<TD>[^<]*</TD>'        # type
                r'<TD>([^<]*)</TD>'       # caid
                r'<TD>([^<]*)</TD>'       # provid
                r'<TD>[^<]*</TD>'         # id
                r'<TD>[^<]*</TD>'         # class
                r'<TD>[^<]*</TD>'         # start date
                r'<TD>([^<]*)</TD>',      # expire date
                resp.text,
            ):
                status, caid, provid, exp_date = match.groups()
                entitlements.append(EntitlementInfo(
                    caid=caid.strip(),
                    provid=provid.strip(),
                    exp_date=exp_date.strip(),
                    active=(status == "e_valid"),
                ))
            return entitlements

        except Exception as e:
            logger.warning(f"Error scraping entitlements for {reader_label}: {e}")
            return []

    async def get_reader_info_with_entitlements(self) -> list[ReaderInfo]:
        """Get readers and enrich with entitlement counts."""
        readers = await self.get_readers()

        for reader in readers:
            entitlements = await self.get_entitlements(reader.label)
            reader.entitlements_count = sum(1 for e in entitlements if e.active)

        return readers

    async def get_log(self) -> list[str]:
        """Get the current log buffer from OScam."""
        root = await self._get_xml("status", appendlog="1")
        if root is None:
            return []

        log_el = root.find(".//log")
        if log_el is None:
            return []

        log_text = log_el.text or ""
        lines = [line.strip() for line in log_text.strip().split("\n") if line.strip()]
        return lines

    async def get_user_stats(self) -> list[dict]:
        """Get per-user statistics from OScam."""
        root = await self._get_xml("userstats")
        if root is None:
            return []

        users = []
        for user_el in root.findall(".//user"):
            users.append({
                "username": user_el.findtext("name", "") or user_el.get("name", ""),
                "status": user_el.findtext("status", ""),
                "ip": user_el.findtext("ip", ""),
                "protocol": user_el.findtext("protocol", ""),
                "ecms_ok": int(user_el.findtext("cwok", "0") or "0"),
                "ecms_nok": int(user_el.findtext("cwnok", "0") or "0"),
                "idle_seconds": int(user_el.findtext("idle", "0") or "0"),
            })

        return users

    async def is_connected(self) -> bool:
        """Check if we can connect to OScam."""
        root = await self._get_xml("status")
        return root is not None
