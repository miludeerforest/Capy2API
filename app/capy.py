"""Capy web-session client.

Targets the *web app* API (https://api.capy.ai/app/...) which is what serves the
ad-sponsored free models. The documented public API (/api/v1, capy_ API keys)
bills credits and is a different route — see README.

Auth is a Clerk session JWT. Clerk session tokens live ~60s, so the client mints
short-lived JWTs from a stored Clerk client cookie instead of storing a bearer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import tempfile
from typing import Any, AsyncIterator
import uuid

import httpx

API_BASE = os.environ.get("CAPY_API_BASE", "https://api.capy.ai").rstrip("/")
CLERK_BASE = os.environ.get("CAPY_CLERK_BASE", "https://clerk.capy.ai").rstrip("/")
CLERK_JS_VERSION = os.environ.get("CAPY_CLERK_JS_VERSION", "6.43.0")
WEB_ORIGIN = os.environ.get("CAPY_WEB_ORIGIN", "https://capy.ai").rstrip("/")
USER_AGENT = os.environ.get(
    "CAPY_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
)

# The web client declares these on every /app/* request; the server gates
# feature access (ad-sponsored models included) on x-capy-client-features.
CLIENT_FEATURES = os.environ.get("CAPY_CLIENT_FEATURES", "message-edit,visual,ads-models")
CLIENT_SURFACE = os.environ.get("CAPY_CLIENT_SURFACE", "web")
CLIENT_TIMEZONE = os.environ.get("CAPY_CLIENT_TIMEZONE", "Asia/Shanghai")
CLIENT_ID = os.environ.get("CAPY_CLIENT_ID") or uuid.uuid4().hex
STATE_PATH = os.environ.get("CAPY_STATE_PATH", "/data/state.json")


class CapyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


class CapySession:
    """Holds Clerk credentials and exposes the web app API surface."""

    def __init__(
        self,
        cookie: str | None = None,
        session_id: str | None = None,
        bearer: str | None = None,
        org_id: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.cookie = cookie or os.environ.get("CAPY_COOKIE", "")
        self.session_id = session_id or os.environ.get("CAPY_CLERK_SESSION_ID", "")
        self._static_bearer = bearer or os.environ.get("CAPY_BEARER", "")
        self.org_id = org_id or os.environ.get("CAPY_ORG_ID", "")
        self._token = self._static_bearer
        self._token_exp = _jwt_exp(self._static_bearer) if self._static_bearer else 0.0
        self._lock = asyncio.Lock()
        self._state = self._load_state()
        self.cookie = self.cookie or self._state.get("cookie", "")
        self.session_id = self.session_id or self._state.get("session_id", "")
        self.org_id = self.org_id or self._state.get("org_id", "")
        if not self._static_bearer and self._state.get("bearer"):
            self._static_bearer = self._state["bearer"]
            self._token = self._static_bearer
            self._token_exp = _jwt_exp(self._static_bearer)
        self.client_id = self._state.get("client_id") or CLIENT_ID
        self._http = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "user-agent": USER_AGENT,
                "origin": WEB_ORIGIN,
                "referer": WEB_ORIGIN + "/",
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "x-capy-client-features": CLIENT_FEATURES,
                "x-capy-client-surface": CLIENT_SURFACE,
                "x-capy-client-timezone": CLIENT_TIMEZONE,
                "x-capy-client-id": self.client_id,
            },
        )
        self._persist()

    # --------------------------------------------------------------- state

    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _persist(self) -> None:
        payload = {
            "cookie": self.cookie,
            "session_id": self.session_id,
            "org_id": self.org_id,
            "client_id": self.client_id,
        }
        if self._static_bearer:
            payload["bearer"] = self._static_bearer
        directory = os.path.dirname(STATE_PATH) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".capy-state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(temporary, 0o600)
            os.replace(temporary, STATE_PATH)
        except OSError as e:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise CapyError("could not persist session credentials", 503) from e

    def ingest(self, cookie: str | None, bearer: str | None, org_id: str | None) -> dict:
        """Accept a `__client` cookie (durable) or a short-lived Clerk JWT."""
        self._http.cookies.clear()
        if cookie or bearer:
            self.org_id = ""
        if cookie:
            self.cookie = cookie.strip()
            self.session_id = ""
            # A cookie mints its own JWTs; drop any stale static bearer.
            self._static_bearer = ""
            self._token = ""
            self._token_exp = 0.0
        if bearer:
            self.cookie = ""
            self.session_id = ""
            self._static_bearer = bearer.strip()
            self._token = self._static_bearer
            self._token_exp = _jwt_exp(self._static_bearer)
        if org_id:
            self.org_id = org_id.strip()
        self._persist()
        return {
            "cookie_set": bool(self.cookie),
            "bearer_set": bool(self._static_bearer),
            "bearer_expires_in": max(0, int(self._token_exp - time.time())),
            "session_id": self.session_id or None,
            "org_id": self.org_id or None,
        }

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- auth

    async def token(self) -> str:
        async with self._lock:
            if self._token and self._token_exp - 20 > time.time():
                return self._token
            if not self.cookie:
                if self._static_bearer and self._token_exp > time.time():
                    return self._static_bearer
                raise CapyError("missing or expired session credentials; ingest a Clerk cookie", 503)
            sid = self.session_id or await self.discover_session_id()
            url = f"{CLERK_BASE}/v1/client/sessions/{sid}/tokens"
            r = await self._http.post(
                url,
                params={"_clerk_js_version": CLERK_JS_VERSION},
                headers={"cookie": self.cookie, "origin": WEB_ORIGIN},
            )
            if r.status_code != 200:
                raise CapyError(f"clerk token failed: {r.status_code}", r.status_code, r.text[:400])
            jwt = r.json().get("jwt")
            if not jwt:
                raise CapyError("clerk token response had no jwt", body=r.text[:400])
            self._token = jwt
            self._token_exp = _jwt_exp(jwt) or time.time() + 55
            return jwt

    async def discover_session_id(self) -> str:
        """Pick the Clerk session id from the __client cookie (no sid needed in env)."""
        r = await self._http.get(
            f"{CLERK_BASE}/v1/client",
            params={"_clerk_api_version": "2026-08-20", "_clerk_js_version": CLERK_JS_VERSION},
            headers={"cookie": self.cookie, "origin": WEB_ORIGIN},
        )
        if r.status_code != 200:
            raise CapyError(f"clerk client failed: {r.status_code}", r.status_code, r.text[:400])
        sessions = (r.json().get("response") or {}).get("sessions") or []
        active = [s for s in sessions if s.get("status") == "active"]
        if not active:
            raise CapyError("cookie has no active Clerk session; log in again", 503)
        self.session_id = active[0]["id"]
        self._persist()
        return self.session_id

    async def _headers(self, extra: dict | None = None) -> dict:
        h = {"authorization": f"Bearer {await self.token()}"}
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------ transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        raw: bool = False,
    ) -> Any:
        if not path.startswith("/app/") or "\\" in path or "#" in path:
            raise CapyError("only relative /app/ paths are permitted", 400)
        url = API_BASE + path
        try:
            r = await self._http.request(
                method, url, params=params, json=json_body, headers=await self._headers()
            )
        except httpx.HTTPError as e:
            raise CapyError(f"upstream transport failed ({type(e).__name__})", 502) from e
        if not 200 <= r.status_code < 300:
            raise CapyError(f"upstream returned HTTP {r.status_code}", r.status_code, r.text[:800])
        if raw:
            return r
        try:
            return r.json()
        except Exception:
            return r.text

    async def get(self, path: str, **kw) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json_body: Any = None, **kw) -> Any:
        return await self.request("POST", path, json_body=json_body, **kw)

    async def sse(self, path: str, params: dict | None = None) -> AsyncIterator[dict]:
        """Yield decoded JSON events from an SSE endpoint."""
        url = API_BASE + path
        headers = await self._headers({"accept": "text/event-stream"})
        async with self._http.stream("GET", url, params=params, headers=headers) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "replace")
                raise CapyError(f"GET {path} -> {r.status_code}", r.status_code, body[:800])
            data: list[str] = []
            async for line in r.aiter_lines():
                if line == "":
                    if data:
                        blob = "\n".join(data)
                        data.clear()
                        try:
                            yield json.loads(blob)
                        except Exception:
                            yield {"_raw": blob}
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                blob = "\n".join(data)
                try:
                    yield json.loads(blob)
                except Exception:
                    yield {"_raw": blob}

    # ---------------------------------------------------------- app surface

    async def me(self) -> Any:
        return await self.get("/app/me")

    async def organizations(self) -> Any:
        return await self.get("/app/organizations")

    async def models(self) -> Any:
        return await self.get("/app/models")

    async def resolve_org(self) -> str:
        """Use CAPY_ORG_ID when set, else the account's first organization."""
        if self.org_id:
            return self.org_id
        raw = await self.organizations()
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list) or not items:
            raise CapyError(f"no organization on this account: {str(raw)[:300]}")
        self.org_id = items[0].get("id") or items[0].get("orgId")
        if not self.org_id:
            raise CapyError(f"organization has no id: {str(items[0])[:300]}")
        return self.org_id

    async def _org(self, org_id: str | None = None) -> str:
        return org_id or await self.resolve_org()

    async def create_thread(self, payload: dict, org_id: str | None = None) -> Any:
        return await self.post(f"/app/orgs/{await self._org(org_id)}/jams", payload)

    async def thread(self, jam_id: str, org_id: str | None = None) -> Any:
        return await self.get(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}")

    async def thread_ad(self, jam_id: str, org_id: str | None = None) -> Any:
        return await self.get(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}/ad")

    async def thread_usage(self, jam_id: str, org_id: str | None = None) -> Any:
        return await self.get(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}/usage")

    async def send_message(self, jam_id: str, payload: dict, org_id: str | None = None) -> Any:
        return await self.post(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}/message", payload)

    async def interrupt(self, jam_id: str, org_id: str | None = None) -> Any:
        return await self.post(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}/interrupt", {})

    async def delete_threads(self, ids: list[str], org_id: str | None = None) -> Any:
        return await self.post(f"/app/orgs/{await self._org(org_id)}/jams/delete", {"jamIds": ids})

    def thread_events(self, jam_id: str, org_id: str | None = None) -> AsyncIterator[dict]:
        async def gen():
            async for ev in self.sse(f"/app/orgs/{await self._org(org_id)}/jams/{jam_id}/events"):
                yield ev

        return gen()
