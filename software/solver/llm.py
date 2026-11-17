"""
A small client for llama-server's OpenAI-compatible API (streaming), with cancel, timeouts and
token/speed accounting. Only ever talks to the configured local llama-server.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    tokens: int
    seconds: float                 # first token → last token
    finish: str
    think: str = ""

    @property
    def tps(self) -> float:
        return self.tokens / self.seconds if self.seconds > 0 else 0.0


class LLM:
    def __init__(self, base_url: str, token_timeout: float = 300.0, total_timeout: float = 1800.0):
        self.base = base_url.rstrip("/")
        self.token_timeout = token_timeout
        self.total_timeout = total_timeout

    def _get(self, path: str, timeout: float = 3.0):
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
                if r.status != 200:
                    return None
                return json.loads(r.read(1 << 20))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False

    def props(self) -> dict:
        p = self._get("/props")
        return p if isinstance(p, dict) else {}

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.2,
             cancel: threading.Event | None = None, on_token=None, extra: dict | None = None) -> ChatResult:
        body = {"messages": messages, "stream": True, "temperature": temperature, "max_tokens": int(max_tokens),
                "top_p": 0.9, "cache_prompt": True}
        if extra:
            body.update(extra)
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        parts, think, tokens, first, last, finish = [], [], 0, None, None, ""
        deadline = time.monotonic() + self.total_timeout
        try:
            with urllib.request.urlopen(req, timeout=self.token_timeout) as r:
                for raw in r:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    if time.monotonic() > deadline:
                        finish = "timeout"
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        ch = obj["choices"][0] if obj.get("choices") else {}
                        delta = ch.get("delta") or {}
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError):
                        continue
                    if not isinstance(delta, dict):
                        continue
                    c, rc = delta.get("content"), delta.get("reasoning_content")
                    got = False
                    if isinstance(c, str) and c:
                        parts.append(c)
                        got = True
                    if isinstance(rc, str) and rc:
                        think.append(rc)
                        got = True
                    if got:
                        tokens += 1
                        now = time.monotonic()
                        first = first or now
                        last = now
                        if on_token:
                            on_token(tokens)
                        if sum(map(len, parts)) + sum(map(len, think)) > 200_000:
                            finish = "length"
                            break
                    if isinstance(ch, dict) and ch.get("finish_reason"):
                        finish = str(ch["finish_reason"])[:20]
        except Cancelled:
            raise
        except urllib.error.HTTPError as e:
            raise LLMError(f"the AI model refused the request (HTTP {e.code})") from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if cancel is not None and cancel.is_set():
                raise Cancelled from None
            raise LLMError(f"the AI model isn't answering ({type(e).__name__})") from None
        secs = (last - first) if first and last else 0.0
        text = "".join(parts)
        th = "".join(think)
        if th:
            text = "<think>" + th + "</think>" + text
        return ChatResult(text=text, tokens=tokens, seconds=secs, finish=finish, think=th)
