"""Per-request Capy realtime driver. Never shares upstream conversation history."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from .capy import CapyError, CapySession, WEB_ORIGIN
from .protocol import ToolDecisionError, ToolRequest, strict_json

try:
    from websockets.asyncio.client import connect
except ImportError:  # Optional transport already supplied by uvicorn[standard].
    connect = None


log = logging.getLogger("capy2api.engine")
REALTIME_URL = "wss://realtime-production.cloud.capy.ai/realtime"
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")


def usage_counts(usage: dict) -> dict:
    keys = (*USAGE_KEYS, "nonCachedInputTokens", "cacheReadInputTokens", "cacheWriteInputTokens", "totalTokens")
    return {key: usage[key] for key in keys if type(usage.get(key)) in (int, float) and usage[key] >= 0}


def sum_usage(steps: list[dict]) -> dict:
    aliases = {"cacheReadTokens": "cacheReadInputTokens", "cacheWriteTokens": "cacheWriteInputTokens"}
    return {
        key: sum(value for step in steps
                 if type(value := step.get(key, step.get(aliases.get(key, ""), 0))) in (int, float))
        for key in USAGE_KEYS
    }


def committed_result(events: dict) -> tuple[str, dict, list[dict]]:
    texts, answer, steps, seen = [], "", [], set()
    for item in events.get("items") or []:
        if item.get("kind") != "assistant" or item.get("state") != "committed":
            continue
        if item.get("id") and item["id"] in seen:
            continue
        seen.add(item.get("id"))
        payload = item.get("payload") or {}
        parts = []
        for block in payload.get("blocks") or []:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_use" and block.get("toolName") == "message_user":
                args = block.get("args") or {}
                if isinstance(args.get("text"), str) and args.get("state") in (None, "final"):
                    answer = args["text"]
        if "".join(parts).strip():
            texts.append("".join(parts))
        if isinstance(payload.get("usage"), dict):
            steps.append(usage_counts(payload["usage"]))
    return answer or (texts[-1] if texts else ""), sum_usage(steps), steps


def string_prefix(text: str, start: int) -> tuple[str, int, bool]:
    """Decode a JSON string prefix, withholding incomplete escapes/surrogates."""
    pos, output = start + 1, []
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while pos < len(text):
        char = text[pos]
        if char == '"':
            return "".join(output), pos + 1, True
        if char == "\\":
            if pos + 1 >= len(text):
                break
            code = text[pos + 1]
            if code in escapes:
                output.append(escapes[code]); pos += 2; continue
            if code != "u":
                raise ValueError("invalid JSON escape")
            if pos + 6 > len(text):
                break
            point = int(text[pos + 2:pos + 6], 16)
            size = 6
            if 0xD800 <= point <= 0xDBFF:
                if pos + 12 > len(text):
                    break
                if text[pos + 6:pos + 8] != "\\u":
                    raise ValueError("invalid surrogate pair")
                low = int(text[pos + 8:pos + 12], 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise ValueError("invalid surrogate pair")
                point = 0x10000 + ((point - 0xD800) << 10) + low - 0xDC00
                size = 12
            elif 0xDC00 <= point <= 0xDFFF:
                raise ValueError("unpaired surrogate")
            output.append(chr(point)); pos += size; continue
        if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError("invalid JSON character")
        output.append(char); pos += 1
    return "".join(output), pos, False


def string_fields(text: str) -> dict[str, str]:
    """Read only root string fields; this never authorizes a tool invocation."""
    fields, seen, pos = {}, set(), 0
    decoder = json.JSONDecoder()
    try:
        text = text.lstrip()
        if not text.startswith("{"):
            return fields
        pos = 1
        while pos < len(text):
            while pos < len(text) and text[pos].isspace(): pos += 1
            if pos == len(text) or text[pos] != '"': break
            key, pos, complete = string_prefix(text, pos)
            if not complete: break
            if key in seen: return {}
            seen.add(key)
            while pos < len(text) and text[pos].isspace(): pos += 1
            if pos == len(text) or text[pos] != ":": break
            pos += 1
            while pos < len(text) and text[pos].isspace(): pos += 1
            if pos == len(text): break
            if text[pos] == '"':
                value, pos, complete = string_prefix(text, pos)
                fields[key] = value
                if not complete: break
            else:
                _, pos = decoder.raw_decode(text, pos)
            while pos < len(text) and text[pos].isspace(): pos += 1
            if pos == len(text) or text[pos] != ",": break
            pos += 1
    except (ValueError, RecursionError):
        return {}
    return fields


class TurnEngine:
    def __init__(self, client: CapySession, org: str, project: str, model: str,
                 decision: ToolRequest, mode: str | None, trace: dict, *,
                 timeout: float, poll_interval: float, reconcile_interval: float,
                 realtime: bool, delete_after: bool,
                 emit: Callable[[str], Awaitable[None]] | None = None):
        self.client, self.org, self.project, self.model = client, org, project, model
        self.decision, self.mode, self.trace = decision, mode, trace
        self.timeout, self.poll_interval = timeout, poll_interval
        self.reconcile_interval, self.realtime = reconcile_interval, realtime
        self.delete_after, self.emit = delete_after, emit
        self.jam_id = "jam_" + str(uuid.uuid4())
        self.base = f"/app/orgs/{org}/jams/{self.jam_id}"
        self.attempted = self.idle = self.caught_start = self.checked_model = False
        self.ws = None
        self.texts: dict[str, str] = {}
        self.inputs: dict[str, str] = {}
        self.delivery = ""
        self.sent = ""
        self.steps: list[dict] = []
        self.current_usage = False
        self.trace.update(thread_id=self.jam_id, rest_polls=0, ws_events=0,
                          content_chunks=0, cleanup={"state": "pending"})

    def mark(self, name: str):
        self.trace.setdefault(name, round((time.perf_counter() - self.trace["_start"]) * 1000, 2))

    def check_model(self, jam: dict):
        for field in ("model", "servedModel"):
            selection = jam.get(field) or {}
            if selection.get("modelId") and selection["modelId"] != self.model:
                raise CapyError("upstream selected a different model; turn interrupted", 502)
        if jam.get("runStatus") in ("error", "cancelled"):
            raise CapyError("upstream turn failed or was cancelled", 502)
        self.idle = jam.get("runStatus") in ("idle", "done", "complete", "finished")

    async def verify_model(self):
        if not self.checked_model:
            self.check_model(await self.client.get(self.base))
            self.checked_model = True

    async def emit_prefix(self, text: str):
        if self.emit is None or not text:
            return
        if not text.startswith(self.sent):
            raise ToolDecisionError("stream_content_changed")
        delta = text[len(self.sent):]
        if delta:
            await self.verify_model()
            self.mark("first_content_ms")
            await self.emit(delta)
            self.sent = text
            self.trace["content_chunks"] += 1

    async def preview(self, text: str, *, delivery: bool = False):
        if self.emit is None:
            return
        if self.decision.nonce is None:
            if delivery:
                await self.emit_prefix(text)
            return
        fields = string_fields(text)
        if (fields.get("protocol") == "capy2api.tools.v1"
                and fields.get("request_id") == self.decision.nonce
                and fields.get("kind") == "final"
                and self.decision.choice in ("auto", "none")):
            await self.emit_prefix(fields.get("content", ""))

    async def finish(self, text: str, usage: dict, steps: list[dict], source: str) -> dict:
        message, reason = self.decision.decode(text)
        if source == "realtime_steps":
            # REST idle may be stale from before the turn actually started.
            self.idle = False
        await self.verify_model()
        if message.get("tool_calls"):
            if self.sent: raise ToolDecisionError("tool_call_after_text")
        else:
            await self.emit_prefix(message["content"])
        self.trace.update(completion_source=source, usage_source=source,
                          assistant_steps=len(steps), upstream_steps=steps,
                          upstream_usage=usage, decision_type=reason)
        self.mark("decision_ms")
        return {"message": message, "finish_reason": reason, "upstream_usage": usage}

    async def event(self, frame: dict) -> dict | None:
        if frame.get("jamId") != self.jam_id or frame.get("type") != "delta":
            return None
        self.trace["ws_events"] += 1
        self.mark("first_upstream_ms")
        delta = frame.get("delta") or {}
        if delta.get("type") == "turn-start":
            self.idle = False
            self.caught_start = True
        if delta.get("type") != "llm-event": return None
        event = delta.get("event") or {}
        kind, ident = event.get("type"), event.get("id", "")
        if kind == "fallback":
            raise CapyError("upstream model fallback is not permitted", 502)
        if kind == "step-start":
            self.idle = False
            self.texts, self.inputs, self.delivery = {}, {}, ""
            self.current_usage = False
        elif kind == "text-delta":
            self.texts[ident] = self.texts.get(ident, "") + event.get("text", "")
            await self.preview("".join(self.texts.values()))
        elif kind in ("tool-input-start", "tool-input-delta", "tool-call"):
            if event.get("name") != "message_user":
                raise CapyError("unexpected cloud tool use; client tool decision aborted", 502)
            if kind == "tool-input-start": self.inputs[ident] = ""
            elif kind == "tool-input-delta":
                self.inputs[ident] = self.inputs.get(ident, "") + event.get("text", "")
                args = string_fields(self.inputs[ident])
                if args.get("state") == "final":
                    await self.preview(args.get("text", ""), delivery=True)
            else:
                args = event.get("input") or {}
                if isinstance(args, dict) and args.get("state") == "final" and isinstance(args.get("text"), str):
                    self.delivery = args["text"]
        elif kind in ("step-finish", "finish"):
            if not self.current_usage and isinstance(event.get("usage"), dict):
                self.steps.append(usage_counts(event["usage"]))
                self.current_usage = True
            if not self.caught_start: return None
            candidate = self.delivery
            if not candidate:
                direct = "".join(self.texts.values()).strip()
                if self.decision.nonce is None:
                    candidate = direct
                else:
                    # A complete bound decision can finish before a second agent
                    # pass that only repeats it through message_user.
                    try:
                        value = strict_json(direct)
                        if (isinstance(value, dict) and value.get("request_id") == self.decision.nonce
                                and value.get("protocol") == "capy2api.tools.v1"):
                            candidate = direct
                    except (ValueError, RecursionError): pass
            if candidate:
                return await self.finish(candidate, sum_usage(self.steps), self.steps[:], "realtime_steps")
        return None

    async def poll(self) -> dict | None:
        self.trace["rest_polls"] += 1
        jam, events = await asyncio.gather(self.client.get(self.base), self.client.get(self.base + "/events"))
        self.check_model(jam)
        if events.get("cursor"):
            raise CapyError("event page is incomplete; refusing partial turn output", 502)
        for item in events.get("items") or []:
            if item.get("kind") != "assistant": continue
            payload = item.get("payload") or {}
            selected = payload.get("model") or {}
            if selected.get("modelId") and selected["modelId"] != self.model:
                raise CapyError("upstream event used a different model", 502)
            if any(b.get("type") == "tool_use" and b.get("toolName") != "message_user"
                   for b in payload.get("blocks") or []):
                raise CapyError("unexpected cloud tool use; client tool decision aborted", 502)
        text, usage, steps = committed_result(events)
        if text and self.idle:
            return await self.finish(text, usage, steps, "committed_events")
        return None

    async def close_socket(self):
        if self.ws is not None:
            try: await self.ws.close()
            except Exception: pass
            self.ws = None

    async def run(self) -> dict:
        async with asyncio.timeout(self.timeout):
            if self.realtime and connect is not None:
                started = time.perf_counter()
                try:
                    self.ws = await connect(REALTIME_URL, origin=WEB_ORIGIN, open_timeout=4,
                                            close_timeout=1, max_size=8 * 1024 * 1024)
                    await self.ws.send(json.dumps({"type": "auth", "token": await self.client.token()}))
                    self.trace["transport"] = "realtime"
                except Exception:
                    await self.close_socket()
                    self.trace["transport"] = "poll_fallback"
                self.trace["ws_connect_ms"] = round((time.perf_counter() - started) * 1000, 2)
            else: self.trace["transport"] = "poll"
            body = {"jamId": self.jam_id, "requestId": str(uuid.uuid4()), "title": "capy2api",
                    "message": self.decision.prompt, "model": {"modelId": self.model}}
            if self.mode: body["model"]["reasoningMode"] = self.mode
            self.attempted = True
            started = time.perf_counter()
            # Never retry creation: a lost response can still have created a jam.
            await self.client.post(f"/app/orgs/{self.org}/projects/{self.project}/jams", body)
            self.trace["create_ms"] = round((time.perf_counter() - started) * 1000, 2)
            if self.ws is not None:
                try: await self.ws.send(json.dumps({"type": "subscribe", "scopes": ["jam:" + self.jam_id]}))
                except Exception:
                    await self.close_socket(); self.trace["transport"] = "poll_fallback"
            next_poll, next_auth = time.perf_counter(), time.perf_counter() + 25
            while True:
                now = time.perf_counter()
                if self.ws is not None:
                    try:
                        frame = await asyncio.wait_for(self.ws.recv(), max(0.01, min(next_poll - now, 1)))
                    except TimeoutError: frame = None
                    except Exception:
                        await self.close_socket(); self.caught_start = False
                        self.trace["transport"] = "poll_fallback"; frame = None
                    if frame is not None:
                        try: value = json.loads(frame)
                        except (ValueError, UnicodeError):
                            await self.close_socket(); self.caught_start = False
                            self.trace["transport"] = "poll_fallback"; value = {}
                        result = await self.event(value)
                        if result: return result
                    if self.ws is not None and time.perf_counter() >= next_auth:
                        try:
                            await self.ws.send(json.dumps({"type": "auth", "token": await self.client.token()}))
                            await self.ws.send(json.dumps({"type": "ping"}))
                        except Exception:
                            await self.close_socket(); self.caught_start = False
                            self.trace["transport"] = "poll_fallback"
                        next_auth = time.perf_counter() + 25
                else:
                    await asyncio.sleep(max(0, next_poll - time.perf_counter()))
                if time.perf_counter() >= next_poll:
                    result = await self.poll()
                    if result: return result
                    interval = self.reconcile_interval if self.ws is not None else self.poll_interval
                    next_poll = time.perf_counter() + interval

    async def cleanup(self):
        started = time.perf_counter()
        await self.close_socket()
        state = self.trace["cleanup"]
        if self.attempted:
            actions = []
            if not self.idle or self.trace.get("completion_source") == "realtime_steps":
                actions.append(("interrupt", self.base + "/interrupt", {}))
            if self.delete_after: actions.append(("delete", f"/app/orgs/{self.org}/jams/delete", {"jamIds": [self.jam_id]}))
            for action, path, payload in actions:
                try:
                    async with asyncio.timeout(15): result = await self.client.post(path, payload)
                    if action == "delete" and not any(x.get("jamId") == self.jam_id and x.get("outcome") in ("ok", "not_found") for x in result.get("outcomes", [])):
                        raise CapyError("unconfirmed thread cleanup")
                    state[action] = "ok"
                except Exception as error:
                    state[action] = "failed"
                    log.warning("%s failed for %s (%s)", action, self.jam_id, type(error).__name__)
        state["state"] = "done"
        self.trace["cleanup_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self.mark("total_ms")
