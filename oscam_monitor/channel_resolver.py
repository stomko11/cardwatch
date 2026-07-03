"""Channel resolver - queries OpenWebif API on Enigma2 receivers to get current channel info."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ChannelInfo:
    """Current channel information from a receiver."""

    channel_name: str
    service_ref: str
    sid: str | None = None
    namespace: str | None = None


class OpenWebifClient:
    """Client for OpenWebif API on VU+/Enigma2 receivers."""

    def __init__(self, receiver_ip: str, port: int = 80, username: str | None = None, password: str | None = None, timeout: float = 5.0):
        self.base_url = f"http://{receiver_ip}:{port}"
        self.timeout = timeout
        self.auth = httpx.BasicAuth(username, password) if username and password else None

    async def get_current_channel(self) -> ChannelInfo | None:
        """Get the currently playing channel from the receiver."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {}
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.get(f"{self.base_url}/api/statusinfo", **kwargs)
                if resp.status_code != 200:
                    logger.warning(f"OpenWebif returned {resp.status_code} for {self.base_url}")
                    return None

                data = resp.json()
                service_ref = data.get("currservice_serviceref", "")
                channel_name = data.get("currservice_station", "") or data.get(
                    "currservice_name", ""
                )

                if not channel_name:
                    return None

                # Extract SID from service reference
                # Format: 1:0:1:SID:TSID:ONID:NAMESPACE:0:0:0:
                sid = self._extract_sid(service_ref)

                return ChannelInfo(
                    channel_name=channel_name,
                    service_ref=service_ref,
                    sid=sid,
                )
        except httpx.TimeoutException:
            logger.debug(f"Timeout connecting to receiver at {self.base_url}")
            return None
        except Exception as e:
            logger.debug(f"Error querying receiver at {self.base_url}: {e}")
            return None

    async def get_all_services(self) -> list[dict]:
        """Get all services/bouquets from the receiver (for reference mapping)."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                kwargs = {}
                if self.auth:
                    kwargs["auth"] = self.auth
                resp = await client.get(f"{self.base_url}/api/getservices", **kwargs)
                if resp.status_code != 200:
                    return []

                data = resp.json()
                services = data.get("services", [])
                return services
        except Exception as e:
            logger.debug(f"Error getting services from {self.base_url}: {e}")
            return []

    @staticmethod
    def _extract_sid(service_ref: str) -> str | None:
        """Extract SID (hex) from Enigma2 service reference string."""
        # Service ref format: 1:0:TYPE:SID:TSID:ONID:NAMESPACE:0:0:0:
        parts = service_ref.split(":")
        if len(parts) >= 4:
            try:
                sid_int = int(parts[3], 16)
                return f"{sid_int:04X}"
            except ValueError:
                pass
        return None


async def resolve_channel_for_user(
    username: str,
    user_device_map: dict,
) -> ChannelInfo | None:
    """
    Resolve what channel a user is currently watching.
    Looks up the user's receiver from the mapping, then queries OpenWebif.
    """
    device = user_device_map.get(username)
    if not device:
        logger.debug(f"No receiver mapped for user '{username}'")
        return None

    # Support both old format (plain IP string) and new format (ReceiverDevice object)
    if isinstance(device, str):
        client = OpenWebifClient(device)
    else:
        client = OpenWebifClient(
            device.ip,
            port=device.port,
            username=device.username,
            password=device.password,
        )
    return await client.get_current_channel()


async def resolve_channels_for_all_users(
    user_device_map: dict,
) -> dict[str, ChannelInfo | None]:
    """Resolve current channel for all mapped users."""
    results = {}
    for username, device in user_device_map.items():
        if isinstance(device, str):
            client = OpenWebifClient(device)
        else:
            client = OpenWebifClient(
                device.ip,
                port=device.port,
                username=device.username,
                password=device.password,
            )
        results[username] = await client.get_current_channel()
    return results
