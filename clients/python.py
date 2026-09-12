"""Generic MegaBrain Python client. No Hermes/Router dependency.

Usage:
    from clients.python import MegaBrainClient
    c = MegaBrainClient("http://127.0.0.1:4300", token="...")
"""
from __future__ import annotations

import json
import urllib.request


class MegaBrainError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"MegaBrain HTTP {status}: {body[:200]}")


class MegaBrainClient:
    def __init__(self, base_url: str = "http://127.0.0.1:4300", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise MegaBrainError(e.code, e.read().decode(errors="replace")) from None

    # writes
    def create_project(self, project_id: str, name: str, status: str = "ACTIVE") -> dict:
        return self._request("POST", "/v1/projects", {"project_id": project_id, "name": name, "status": status})

    def write_event(self, event: dict) -> dict:
        return self._request("POST", "/v1/events", event)

    def write_batch(self, events: list[dict]) -> dict:
        return self._request("POST", "/v1/events/batch", {"events": events})

    # reads
    def resolve_project(self, project_id=None, session_id=None, query=None,
                        source="hermes", profile="default", channel="cli",
                        parent_session_id=None, conversation_id=None) -> dict:
        return self._request("POST", "/v1/resolve-project", {
            "project_id": project_id, "session_id": session_id, "query": query,
            "source": source, "profile": profile, "channel": channel,
            "parent_session_id": parent_session_id, "conversation_id": conversation_id})

    def get_context(self, project_id=None, session_id=None, query=None,
                    token_budget=None, since_revision=None) -> dict:
        return self._request("POST", "/v1/memory/context", {
            "project_id": project_id, "session_id": session_id, "query": query,
            "token_budget": token_budget, "since_revision": since_revision})

    def get_event(self, event_id: str) -> dict:
        return self._request("GET", f"/v1/events/{event_id}")

    def health(self) -> dict:
        return self._request("GET", "/health")
