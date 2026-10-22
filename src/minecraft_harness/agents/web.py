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

    def __init__(self, config: dict[str, Any] | None = None, *,
                 budget: Any = None, agent_id: str | None = None):
        self.config = config or {}
        # The Supervisor may pass a dict subclass carrying the team Budget and
        # the bound agent identity; explicit keyword arguments win in tests.
        self.budget = budget if budget is not None else getattr(config, "_web_budget", None)
        self.agent_id = agent_id if agent_id is not None else getattr(config, "_web_agent_id", None)
        self.enabled = bool(self.config.get("enabled", False))
        self.timeout_s = min(60.0, max(1.0, float(self.config.get("timeout_s", 15))))
        self.max_response_bytes = min(1_000_000, max(1024, int(self.config.get("max_response_bytes", 128_000))))
        self.allowed_hosts = {str(host).lower() for host in self.config.get("allowed_hosts", [])}
        self.provider = str(self.config.get("provider", "custom")).lower()
        self.fetch_url = self.config.get("fetch_url")
        self.api_key_env = self.config.get("api_key_env") or self.config.get("search_key_env")
        self.fetch_key_env = self.config.get("fetch_key_env") or self.api_key_env
        self.count = max(1, min(50, int(self.config.get("count", 10))))
        self.need_content = bool(self.config.get("need_content", False))

    def _admit(self, tool: str) -> bool:
        """Reserve one team-wide web call; deny closed (no network) on failure."""
        if self.budget is None:
            return True
        try:
            return bool(self.budget.admit_web(self.agent_id or "agent", tool))
        except Exception:
            # Metering must never raise into the agent loop or stop the run.
            return False

    def _key(self, variable):
        if not variable:
            return None
        value = os.environ.get(str(variable))
        return value if value else None

    async def _official(self, url, payload, key_env, label):
        key = self._key(key_env)
        if not key:
            return {"status": "unavailable", "backend": label, "reason": "API key is unset"}
        result = await self._read("POST", url, json=payload,
                                  headers={"authorization": "Bearer " + key,
                                           "content-type": "application/json"})
        if result.get("status") != "ok":
            return {**result, "backend": label}
        try:
            document = json.loads(result["content"])
        except (TypeError, ValueError):
            return {"status": "unavailable", "backend": label, "reason": "Search/fetch service returned invalid JSON"}
        if not isinstance(document, dict):
            return {"status": "unavailable", "backend": label, "reason": "Search/fetch service returned a non-object"}
        return document

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
        local_search = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
                        and parsed.port not in (None, 443))
        if (parsed.scheme != "https" and not local_search) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Configured search service requires HTTPS, or loopback HTTP for an operator-local adapter")
        headers = {"content-type": "application/json"}
        official = self.provider == "deepseek" or "api.deepseek.com" in (parsed.hostname or "")
        if official:
            if len(query) > 128:
                return {"status": "unavailable", "backend": "deepseek-search",
                        "reason": "DeepSeek search query must be at most 128 characters"}
            # Only requests that can actually leave the process consume the cap.
            if not self._key(self.api_key_env):
                return {"status": "unavailable", "backend": "deepseek-search",
                        "reason": "API key is unset"}
            if not self._admit("web_search"):
                return {"status": "unavailable", "capability": "web_search",
                        "reason": "web call budget exhausted"}
            payload = {"query": query, "count": self.count, "need_content": self.need_content}
            document = await self._official(url, payload, self.api_key_env, "deepseek-search")
            if document.get("status") == "unavailable":
                return document
            results = document.get("results")
            if not isinstance(results, list):
                return {"status": "unavailable", "backend": "deepseek-search", "reason": "Search response has no results array"}
            normalized = []
            for item in results[:self.count]:
                if not isinstance(item, dict):
                    continue
                normalized.append({key: item.get(key) for key in
                                   ("title", "url", "snippet", "summary", "content", "site_name", "date", "authority", "rerank_score")
                                   if key in item})
            return {"status": "ok", "backend": "deepseek-search", "query": query,
                    "source": parsed.hostname, "results": normalized}
        variable = self.config.get("search_key_env")
        if variable:
            key = os.environ.get(variable)
            if not key:
                return {"status": "unavailable", "reason": "Search credential is unset"}
            headers["authorization"] = "Bearer " + key
        if not self._admit("web_search"):
            return {"status": "unavailable", "capability": "web_search",
                    "reason": "web call budget exhausted"}
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
        if self.provider == "deepseek" or "api.deepseek.com" in (parsed.hostname or ""):
            if not self.fetch_url:
                return {"status": "unavailable", "backend": "deepseek-fetch", "reason": "Fetch API URL is unset"}
            if not self._key(self.fetch_key_env):
                return {"status": "unavailable", "backend": "deepseek-fetch",
                        "reason": "API key is unset"}
            if not self._admit("web_fetch"):
                return {"status": "unavailable", "capability": "web_fetch",
                        "reason": "web call budget exhausted"}
            document = await self._official(self.fetch_url, {"url": url}, self.fetch_key_env, "deepseek-fetch")
            if document.get("status") == "unavailable":
                return document
            return {"status": "ok", "backend": "deepseek-fetch", "source": url,
                    "title": str(document.get("title", ""))[:1000],
                    "content": str(document.get("content", ""))}
        if not self._admit("web_fetch"):
            return {"status": "unavailable", "capability": "web_fetch",
                    "reason": "web call budget exhausted"}
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
