"""Limited OpenAI-compatible text bridge over Capy's ad-model web routes.

Observed catalog zero prices and empty per-thread usage are not a guarantee
against future account charges or cloud-machine costs. No paid fallback.
Uses verified realtime events where available; tools are emitted only after full validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .capy import CapyError, CapySession
from .protocol import ToolDecisionError, prepare_request
from .engine import TurnEngine, committed_result

PROXY_KEY = os.environ.get("CAPY_PROXY_KEY", "")
DELETE_AFTER = os.environ.get("CAPY_DELETE_AFTER", "1") == "1"
FREE_ONLY = os.environ.get("CAPY_ONLY_FREE", "1") == "1"
TURN_TIMEOUT = float(os.environ.get("CAPY_TURN_TIMEOUT", "300"))
POLL_INTERVAL = float(os.environ.get("CAPY_POLL_INTERVAL", "1.5"))
DEFAULT_REASONING = os.environ.get("CAPY_REASONING", "")
PROJECT_ID = os.environ.get("CAPY_PROJECT_ID", "")
REALTIME = os.environ.get("CAPY_REALTIME", "1") == "1"
RECONCILE_INTERVAL = max(0.2, float(os.environ.get("CAPY_RECONCILE_INTERVAL", "3")))
HEARTBEAT_INTERVAL = max(0.1, float(os.environ.get("CAPY_HEARTBEAT_INTERVAL", "5")))

app = FastAPI(title="capy2api", version="0.2.0")
session: CapySession | None = None
models_cache: dict[str, Any] = {"at": 0.0, "org": "", "items": []}
TURN_LOCK = asyncio.Lock()
log = logging.getLogger("capy2api")
PROJECT_CACHE: dict[str, tuple[float, str]] = {}
RECENT = deque(maxlen=30)
IN_FLIGHT: set[asyncio.Task] = set()


def client() -> CapySession:
    global session
    if session is None:
        session = CapySession()
    return session


def require_auth(request: Request) -> None:
    if len(PROXY_KEY) < 32 or PROXY_KEY == "change-me":
        raise HTTPException(503, "configure CAPY_PROXY_KEY with at least 32 characters")
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied.encode(), f"Bearer {PROXY_KEY}".encode()):
        raise HTTPException(401, "invalid proxy key")


@app.exception_handler(CapyError)
async def capy_error(request: Request, exc: CapyError):
    status = exc.status if exc.status in (400, 403, 422, 429, 503, 504) else 502
    # Never return raw upstream auth bodies, which may contain session material.
    error_type = "invalid_model_tool_decision" if isinstance(exc, ToolDecisionError) else "upstream_error"
    return JSONResponse({"error": {"message": str(exc), "type": error_type, "code": error_type}}, status_code=status)


async def json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeError) as e:
        raise HTTPException(400, "invalid JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be an object")
    return body


def free_model(model: dict) -> bool:
    pricing = model.get("pricing")
    route = model.get("route") if isinstance(model.get("route"), dict) else {}
    return (
        model.get("ads") is True
        and model.get("auth") == "capy"
        and model.get("byok") is False
        and model.get("unavailable") is None
        and route.get("kind") == "capy"
        and route.get("locked") is False
        and route.get("skipped") is None
        and isinstance(pricing, dict)
        and {"input", "output", "cacheRead"} <= pricing.keys()
        and all(type(v) in (int, float) and v == 0 for v in pricing.values())
    )


def turn_result(events: dict) -> tuple[str, dict]:
    text, usage, _ = committed_result(events)
    return text, usage


def normalize_model(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "object": "model",
        "created": 0,
        "owned_by": m.get("provider") or "capy",
        "name": m.get("name"),
        "capy": {
            "ads": m.get("ads"),
            "auth": m.get("auth"),
            "byok": m.get("byok"),
            "pricing": m.get("pricing"),
            "limits": m.get("limits"),
            "efforts": m.get("efforts"),
            "defaultEffort": m.get("defaultEffort"),
            "unavailable": m.get("unavailable"),
        },
    }


async def fetch_models(force: bool = False) -> list[dict]:
    org = await client().resolve_org()
    if (
        not force
        and models_cache["items"]
        and models_cache["org"] == org
        and time.time() - models_cache["at"] < 600
    ):
        return models_cache["items"]
    raw = await client().request("GET", "/app/models", params={"orgId": org})
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise CapyError(f"unexpected /app/models shape: {str(raw)[:200]}")
    if not FREE_ONLY:
        raise CapyError("paid routing is unsupported; CAPY_ONLY_FREE must be 1", 503)
    items = [m for m in items if isinstance(m, dict) and free_model(m)]
    models_cache.update(at=time.time(), org=org, items=items)
    return items


async def project_id() -> str:
    if PROJECT_ID:
        return PROJECT_ID
    org_id = await client().resolve_org()
    cached = PROJECT_CACHE.get(org_id)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    orgs = await client().organizations()
    items = orgs.get("items") if isinstance(orgs, dict) else orgs
    for org in items or []:
        if org.get("id") == org_id and org.get("defaultProjectId"):
            PROJECT_CACHE[org_id] = (time.monotonic(), org["defaultProjectId"])
            return org["defaultProjectId"]
    raise CapyError("no default project on this organization; set CAPY_PROJECT_ID")


ORDER = ["none", "instant", "minimal", "low", "medium", "high", "xhigh", "max"]


def reasoning_for(model_id: str, requested: str | None, catalog: list[dict]) -> str | None:
    """Clamp a requested effort to the levels this model actually accepts."""
    model = next((m for m in catalog if m.get("id") == model_id), None)
    efforts = (model or {}).get("efforts") or []
    if not efforts:
        return None
    want = requested or DEFAULT_REASONING
    if want in efforts:
        return want
    if want in ORDER:
        idx = ORDER.index(want)
        ranked = sorted(efforts, key=lambda e: abs(ORDER.index(e) - idx) if e in ORDER else 9)
        return ranked[0]
    default = (model or {}).get("defaultEffort")
    return default if default in efforts else ("low" if "low" in efforts else efforts[0])




def public_trace(trace: dict) -> dict:
    return {key: value for key, value in trace.items() if not key.startswith("_")}


def completion_for(model: str, result: dict, trace: dict) -> dict:
    upstream = result["upstream_usage"]
    prompt_tokens = int(upstream.get("inputTokens") or 0)
    output_tokens = int(upstream.get("outputTokens") or 0)
    return {
        "id": trace["request_id"], "object": "chat.completion", "created": trace["created"], "model": model,
        "choices": [{"index": 0, "message": result["message"], "finish_reason": result["finish_reason"]}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": output_tokens,
                  "total_tokens": prompt_tokens + output_tokens,
                  "prompt_tokens_details": {"cached_tokens": int(upstream.get("cacheReadTokens") or 0)},
                  "completion_tokens_details": {"reasoning_tokens": int(upstream.get("reasoningTokens") or 0)}},
        "capy": public_trace(trace),
    }


def start_job(model: str, decision, reasoning, trace: dict, streaming: bool):
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    ready = asyncio.get_running_loop().create_future()
    # Streaming clients consume the queue instead; still retrieve future errors.
    ready.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    async def emit(text: str):
        await queue.put(("text", text))

    async def produce():
        engine = None
        try:
            async with TURN_LOCK:
                trace["queue_ms"] = round((time.perf_counter() - trace["_start"]) * 1000 - trace["prepare_ms"], 2)
                try:
                    before = time.perf_counter()
                    await client().token()
                    trace["auth_ms"] = round((time.perf_counter() - before) * 1000, 2)
                    before = time.perf_counter()
                    catalog = await fetch_models(force=True)
                    trace["catalog_ms"] = round((time.perf_counter() - before) * 1000, 2)
                    if model not in {m.get("id") for m in catalog}:
                        raise HTTPException(400, "model is not an available zero-price ad model for this org")
                    before = time.perf_counter()
                    org, project = await client().resolve_org(), await project_id()
                    trace["project_ms"] = round((time.perf_counter() - before) * 1000, 2)
                    engine = TurnEngine(client(), org, project, model, decision,
                                        reasoning_for(model, reasoning, catalog), trace,
                                        timeout=TURN_TIMEOUT, poll_interval=max(0.2, POLL_INTERVAL),
                                        reconcile_interval=RECONCILE_INTERVAL, realtime=REALTIME,
                                        delete_after=DELETE_AFTER, emit=emit if streaming else None)
                    result = await engine.run()
                    trace.setdefault("first_content_ms", trace["decision_ms"])
                    trace["elapsed_ms"] = trace["decision_ms"]
                    trace["reasoning_effort"] = engine.mode
                    trace["status"] = "ok"
                    completion = completion_for(model, result, trace)
                    if not ready.done(): ready.set_result(completion)
                    if streaming: await queue.put(("result", completion))
                finally:
                    if engine is not None:
                        # Publish the result first, but retain the credential lease
                        # until cleanup completes; ingestion cannot race this task.
                        await engine.cleanup()
        except asyncio.CancelledError:
            trace["status"] = "cancelled"
            if not ready.done(): ready.cancel()
            raise
        except Exception as error:
            if isinstance(error, TimeoutError): error = CapyError("upstream turn timed out", 504)
            if not isinstance(error, (CapyError, HTTPException)):
                log.warning("request failed (%s)", type(error).__name__)
                error = CapyError("internal bridge error", 502)
            trace["status"] = "error"
            trace["error_type"] = type(error).__name__
            if not ready.done(): ready.set_exception(error)
            if streaming: await queue.put(("error", error))
        finally:
            trace.setdefault("total_ms", round((time.perf_counter() - trace["_start"]) * 1000, 2))
            RECENT.append(public_trace(trace))
            log.info("request timing %s", json.dumps(public_trace(trace)))

    task = asyncio.create_task(produce())
    IN_FLIGHT.add(task)
    task.add_done_callback(IN_FLIGHT.discard)
    return ready, queue, task


@app.get("/admin/metrics")
async def metrics(request: Request):
    require_auth(request)
    return {"in_flight": len(IN_FLIGHT), "recent": list(RECENT)}


@app.on_event("shutdown")
async def shutdown():
    tasks = list(IN_FLIGHT)
    for task in tasks: task.cancel()
    if tasks: await asyncio.gather(*tasks, return_exceptions=True)
    if session is not None: await session.aclose()


@app.get("/healthz")
async def healthz():
    # Liveness only. Do not refresh credentials or change the selected org.
    return {"ok": True, "scope": "process"}


@app.get("/admin/status")
async def status(request: Request):
    require_auth(request)
    async with TURN_LOCK:
        items = await fetch_models(force=True)
        return {"authenticated": True, "org_id": await client().resolve_org(),
                "models": [m["id"] for m in items]}


@app.post("/admin/ingest")
async def ingest(request: Request):
    require_auth(request)
    body = await json_object(request)
    for key in ("cookie", "bearer", "org_id"):
        if key in body and (not isinstance(body[key], str) or not body[key].strip()):
            raise HTTPException(400, f"{key} must be nonempty text")
    if not body.get("cookie") and not body.get("bearer"):
        raise HTTPException(400, "cookie or bearer is required")
    if body.get("cookie") and body.get("bearer"):
        raise HTTPException(400, "provide cookie or bearer, not both")
    async with TURN_LOCK:
        info = client().ingest(body.get("cookie"), body.get("bearer"), body.get("org_id"))
        models_cache.update(at=0, org="", items=[])
        PROJECT_CACHE.clear()
        items = await fetch_models(force=True)
        info["models"] = [m["id"] for m in items]
        return info


@app.get("/v1/models")
async def list_models(request: Request):
    require_auth(request)
    async with TURN_LOCK:
        items = await fetch_models()
    return {"object": "list", "data": [normalize_model(m) for m in items]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    require_auth(request)
    body = await json_object(request)
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "model is required")
    for option in ("functions", "function_call", "response_format",
                   "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
                   "presence_penalty", "frequency_penalty", "logprobs", "seed"):
        if body.get(option) is not None:
            raise HTTPException(400, f"{option} is unsupported by the web agent bridge")
    if body.get("n", 1) != 1:
        raise HTTPException(400, "only n=1 is supported")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise HTTPException(400, "stream must be boolean")
    options = body.get("stream_options") or {}
    if not isinstance(options, dict):
        raise HTTPException(400, "stream_options must be an object")
    start = time.perf_counter()
    decision = await asyncio.to_thread(prepare_request, body)
    reasoning = body.get("reasoning_effort")
    if reasoning is not None and not isinstance(reasoning, str):
        raise HTTPException(400, "reasoning_effort must be text")
    trace = {"_start": start, "request_id": "chatcmpl-" + uuid.uuid4().hex[:24],
             "created": int(time.time()), "prompt_chars": len(decision.prompt),
             "tool_count": len(decision.tools), "stream": stream,
             "prepare_ms": round((time.perf_counter() - start) * 1000, 2)}
    trace.update(decision.stats)
    ready, queue, task = start_job(model, decision, reasoning, trace, stream)
    if not stream:
        async def disconnected():
            while not ready.done():
                if await request.is_disconnected():
                    return True
                await asyncio.sleep(0.25)
            return False
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({ready, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if ready in done:
                return ready.result()
            task.cancel()
            raise HTTPException(499, "client disconnected")
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def gen():
        complete, first = False, True
        chunk = {"id": trace["request_id"], "object": "chat.completion.chunk",
                 "created": trace["created"], "model": model}
        def data(choices, **extra):
            return "data: " + json.dumps({**chunk, "choices": choices, **extra}) + "\n\n"
        try:
            # Transport liveness only, not assistant text or a claimed token TTFT.
            yield ": capy2api waiting for upstream\n\n"
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), HEARTBEAT_INTERVAL)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "text":
                    delta = {"content": value}
                    if first: delta["role"] = "assistant"
                    yield data([{"index": 0, "delta": delta, "finish_reason": None}])
                    first = False
                elif kind == "error":
                    complete = True
                    message = value.detail if isinstance(value, HTTPException) else str(value)
                    code = "invalid_model_tool_decision" if isinstance(value, ToolDecisionError) else "upstream_error"
                    yield "data: " + json.dumps({"error": {"message": message, "type": code, "code": code}}) + "\n\n"
                    break
                elif kind == "result":
                    complete = True
                    choice = value["choices"][0]
                    if choice["message"].get("tool_calls"):
                        delta = {"role": "assistant", "tool_calls": [dict(index=i, **call) for i, call in enumerate(choice["message"]["tool_calls"])]}
                        yield data([{"index": 0, "delta": delta, "finish_reason": None}])
                    yield data([{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}])
                    if options.get("include_usage"):
                        yield data([], usage=value["usage"])
                    yield "data: [DONE]\n\n"
                    break
        finally:
            if not complete:
                task.cancel()
            # The tracked task owns cleanup even when the client disconnects.

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/admin/probe")
async def probe(request: Request, path: str, method: str = "GET"):
    """Read-only, fixed-origin diagnostics; no mutations or arbitrary targets."""
    require_auth(request)
    if method.upper() != "GET":
        raise HTTPException(400, "probe permits GET only")
    async with TURN_LOCK:
        org = await client().resolve_org()
        target = urlsplit(path)
        allowed = {"/app/me", "/app/organizations", "/app/models", f"/app/orgs/{org}/billing"}
        jam_path = rf"/app/orgs/{re.escape(org)}/jams/jam_[A-Za-z0-9-]+(?:/(?:events|usage|ad))?"
        if (target.scheme or target.netloc or target.fragment or "%" in path or "\\" in path
                or (target.path not in allowed and not re.fullmatch(jam_path, target.path))):
            raise HTTPException(400, "probe target is not allowlisted")
        if target.query and (target.path != "/app/models" or parse_qs(target.query) != {"orgId": [org]}):
            raise HTTPException(400, "unsupported probe query")
        return await client().get(path)
