"""Self-contained MegaBrain REST client (stdlib only, no megabrain imports).

Canonical client for the Hermes M5 integration. Ships vendored into the Hermes
plugin directory so it has zero dependency on the megabrain repo path.

Endpoints (MegaBrain API v1):
  health, version, write_event, write_batch, resolve_project, get_context,
  memory_search, search_events, get_event, list_projects, get_project,
  get_project_state, create_project, session_project.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class MegaBrainError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"MegaBrain HTTP {status}: {body[:200]}")


class MegaBrainUnavailable(MegaBrainError):
    """Connection-level failure (DNS/TCP/timeout) — distinct from a 4xx/5xx."""


class MegaBrainClient:
    def __init__(self, base_url: str = "http://127.0.0.1:4300",
                 token: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- low-level ----------------------------------------------------------

    def _request(self, method: str, path: str,
                 body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise MegaBrainError(e.code, e.read().decode(errors="replace")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise MegaBrainUnavailable(0, str(e)) from None

    # -- writes -------------------------------------------------------------

    def write_event(self, event: dict) -> dict:
        return self._request("POST", "/v1/events", event)

    def write_batch(self, events: list[dict]) -> dict:
        return self._request("POST", "/v1/events/batch", {"events": events})

    # -- reads --------------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", "/health")

    def version(self) -> dict:
        return self._request("GET", "/version")

    def resolve_project(self, project_id: str | None = None,
                        session_id: str | None = None,
                        query: str | None = None,
                        source: str = "hermes", profile: str = "default", channel: str = "cli",
                        parent_session_id: str | None = None,
                        conversation_id: str | None = None) -> dict:
        return self._request("POST", "/v1/resolve-project", {
            "project_id": project_id, "session_id": session_id, "query": query,
            "source": source, "profile": profile, "channel": channel,
            "parent_session_id": parent_session_id, "conversation_id": conversation_id})

    def get_context(self, project_id: str | None = None,
                    session_id: str | None = None,
                    query: str | None = None,
                    token_budget: int | None = None,
                    since_revision: int | None = None) -> dict:
        return self._request("POST", "/v1/memory/context", {
            "project_id": project_id, "session_id": session_id, "query": query,
            "token_budget": token_budget, "since_revision": since_revision})

    def memory_search(self, query: str, project_id: str | None = None,
                      session_id: str | None = None,
                      mode: str | None = None, limit: int = 10,
                      at_time: str | None = None) -> dict:
        return self._request("POST", "/v1/memory/search", {
            "query": query, "project_id": project_id, "session_id": session_id,
            "mode": mode, "limit": limit, "at_time": at_time})

    def search_events(self, q: str | None = None,
                      session_id: str | None = None,
                      project_id: str | None = None,
                      event_type: str | None = None,
                      source: str | None = None, limit: int = 50) -> dict:
        params = []
        if q:
            params.append(f"q={urllib.parse.quote(q)}")
        if session_id:
            params.append(f"session_id={urllib.parse.quote(session_id)}")
        if project_id:
            params.append(f"project_id={urllib.parse.quote(project_id)}")
        if event_type:
            params.append(f"event_type={urllib.parse.quote(event_type)}")
        if source:
            params.append(f"source={urllib.parse.quote(source)}")
        params.append(f"limit={limit}")
        return self._request("GET", "/v1/search?" + "&".join(params))

    def get_event(self, event_id: str) -> dict:
        return self._request("GET", f"/v1/events/{urllib.parse.quote(event_id)}")

    # -- projects / sessions ------------------------------------------------

    def list_projects(self) -> dict:
        return self._request("GET", "/v1/projects")

    def get_project(self, project_id: str) -> dict:
        return self._request("GET", f"/v1/projects/{urllib.parse.quote(project_id)}")

    def get_project_state(self, project_id: str) -> dict:
        return self._request(
            "GET", f"/v1/projects/{urllib.parse.quote(project_id)}/state")

    def create_project(self, project_id: str, name: str,
                       status: str = "ACTIVE") -> dict:
        return self._request("POST", "/v1/projects",
                             {"project_id": project_id, "name": name,
                              "status": status})

    def session_project(self, session_id: str) -> dict:
        return self._request(
            "GET", f"/v1/sessions/{urllib.parse.quote(session_id)}/project")
