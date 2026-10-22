#!/usr/bin/env python3
"""Loopback-only HTTP search adapter for the coding agents.

The experiment's players never receive a browser or arbitrary network socket;
the supervisor exposes this bounded operator service through the existing
web_search tool. It queries a public HTML search endpoint through the operator
proxy and returns compact, untrusted results.
"""
from __future__ import annotations
import html
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

PROXY = os.environ.get("SEARCH_UPSTREAM_PROXY", "http://127.0.0.1:24448")
RESULTS = re.compile(r'<li class="b_algo".*?</li>', re.S)
TITLE = re.compile(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
SNIPPET = re.compile(r'<p[^>]*>(.*?)</p>', re.S)
TAG = re.compile(r"<[^>]+>")


def clean(value: str) -> str:
    return html.unescape(TAG.sub("", value or "")).strip()


def search(query: str) -> list[dict]:
    with httpx.Client(proxies=PROXY, timeout=15.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 MinecraftHarness observation search"}) as client:
        response = client.get("https://www.bing.com/search", params={"q": query})
        response.raise_for_status()
    output = []
    for block in RESULTS.findall(response.text)[:8]:
        match = TITLE.search(block)
        if not match:
            continue
        href, title = match.groups()
        snippet = SNIPPET.search(block)
        output.append({"title": clean(title)[:300], "url": href[:1000],
                       "snippet": (clean(snippet.group(1))[:600] if snippet else "")})
    return output


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok"})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/search":
            self.send_error(404)
            return
        try:
            length = min(32_000, max(0, int(self.headers.get("content-length", "0"))))
            body = json.loads(self.rfile.read(length) or b"{}")
            query = str(body.get("query", ""))
            if not 1 <= len(query) <= 1000:
                raise ValueError("query length")
            self._json({"status": "ok", "query": query, "results": search(query)})
        except Exception as error:
            self._json({"status": "unavailable", "error": str(error)[:300]}, 503)

    def _json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", 8899), Handler).serve_forever()
