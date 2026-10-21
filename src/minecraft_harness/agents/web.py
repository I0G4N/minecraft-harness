from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.parse import urlsplit


class WebTools:
    """Bounded external reference access, separate from the game execution network.

    Fetch destinations must be explicitly allowed public HTTPS hosts. Search is an
    operator-configured POST endpoint; its credential is never returned or recorded.
    Redirects are rejected so they cannot bypass the configured host boundary.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.timeout_s = min(60.0, max(1.0, float(self.config.get("timeout_s", 15))))
        self.max_response_bytes = min(1_000_000, max(1024, int(self.config.get("max_response_bytes", 128_000))))
        self.allowed_hosts = {str(host).lower() for host in self.config.get("allowed_hosts", [])}

    async def _read(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        import httpx
        try:
            async with asyncio.timeout(self.timeout_s):
                async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, url, **kwargs) as response:
                        if response.status_code >= 300:
                            return {"status": "unavailable", "http_status": response.status_code}
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.max_response_bytes:
                                return {"status": "oversize", "truncated": True,
                                        "content": bytes(data).decode("utf-8", errors="replace")}
                            data.extend(chunk)
                        return {"status": "ok", "content": bytes(data).decode("utf-8", errors="replace"),
                                "content_type": response.headers.get("content-type", "")[:200], "truncated": False}
        except (TimeoutError, httpx.HTTPError, ValueError):
            return {"status": "unavailable", "error": "Reference request failed"}

    async def search(self, query: str) -> dict[str, Any]:
        if not self.enabled or not self.config.get("search_url"):
            return {"status": "unavailable", "capability": "web_search", "reason": "Search service is not configured"}
        if not isinstance(query, str) or not 1 <= len(query) <= 1000:
            raise ValueError("Search query must contain 1 to 1000 characters")
        url = self.config["search_url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Configured search service requires an HTTPS URL without credentials")
        headers = {"content-type": "application/json"}
        variable = self.config.get("search_key_env")
        if variable:
            key = os.environ.get(variable)
            if not key:
                return {"status": "unavailable", "reason": "Search credential is unset"}
            headers["authorization"] = "Bearer " + key
        result = await self._read("POST", url, json={"query": query}, headers=headers)
        return {**result, "query": query, "source": parsed.hostname}

    async def fetch(self, url: str) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "unavailable", "capability": "web_fetch"}
        if not isinstance(url, str) or len(url) > 4096:
            raise ValueError("Reference URL is too long")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)
                or parsed.hostname.lower() not in self.allowed_hosts):
            raise PermissionError("Fetch requires an explicitly allowed public HTTPS host")
        try:
            addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, 443, type=socket.SOCK_STREAM), self.timeout_s)
        except (OSError, TimeoutError):
            return {"status": "unavailable", "source": url, "error": "Reference hostname could not be resolved"}
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise PermissionError("Private or local fetch destinations are forbidden")
        # Use the already-validated address, retaining Host and TLS SNI. A second
        # DNS resolution must not rebind the request to a private destination.
        address = addresses[0][4][0]
        authority = f"[{address}]" if ":" in address else address
        pinned = parsed._replace(netloc=authority).geturl()
        return {**await self._read("GET", pinned, headers={"host": parsed.hostname},
                                 extensions={"sni_hostname": parsed.hostname}), "source": url}
