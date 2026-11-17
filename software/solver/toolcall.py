"""
Defensive parsing of the model's replies. The model may answer with

    <tool_call>{"tool": "calc", "args": {"expr": "2+2"}}</tool_call>
    <answer> … </answer>
    <plan> … </plan>

and small models wander: Qwen's native {"name": …, "arguments": "…json string…"}, code fences,
trailing commas, single quotes, <think> blocks (closed, unclosed or only a closing tag), several
calls at once, megabytes of junk, deeply nested JSON, NaN. Everything is bounded; nothing here
raises on any input. Only the model's own reply is parsed — never tool results or articles.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_REPLY = 40_000
MAX_JSON = 12_000
MAX_DEPTH = 12
NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass
class Parsed:
    kind: str                         # "tool" | "answer" | "plan" | "none"
    text: str = ""                    # answer/plan text, or the visible text
    tool: str = ""
    args: dict = field(default_factory=dict)
    error: str = ""                   # a tool call was attempted but is unusable
    think: str = ""
    think_truncated: bool = False
    extra_calls: int = 0              # further tool calls in the same reply (ignored)


_THINK = re.compile(r"<think>(.*?)</think>", re.S | re.I)


def split_think(text: str) -> tuple[str, str, bool]:
    """(visible, thinking, truncated)."""
    thinks = [m.group(1) for m in _THINK.finditer(text)]
    vis = _THINK.sub("", text)
    low = vis.lower()
    if "</think>" in low:                         # the template opened <think> for the model
        i = low.rfind("</think>")
        thinks.insert(0, vis[:i])
        vis = vis[i + len("</think>"):]
        low = vis.lower()
    if "<think>" in low:                          # ran out of tokens while thinking
        i = low.find("<think>")
        thinks.append(vis[i + 7:])
        return vis[:i].strip(), "\n".join(thinks).strip(), True
    return vis.strip(), "\n".join(thinks).strip(), False


def _scan_object(s: str, start: int) -> tuple[str | None, str]:
    """The balanced {...} starting at s[start] (string-aware), or (None, reason)."""
    depth, maxd, i, n = 0, 0, start, len(s)
    in_str, esc, quote = False, False, ""
    end_limit = min(n, start + MAX_JSON)
    while i < end_limit:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c in "{[":
            depth += 1
            maxd = max(maxd, depth)
            if maxd > MAX_DEPTH:
                return None, "the tool call is nested too deeply"
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return s[start:i + 1], ""
            if depth < 0:
                return None, "unbalanced brackets"
        i += 1
    return None, "the tool call is incomplete or too long"


def _reject_constant(x):
    raise ValueError(f"{x} is not allowed")


def _loads(s: str):
    try:
        return json.loads(s, parse_constant=_reject_constant), ""
    except RecursionError:
        return None, "nested too deeply"
    except ValueError as e:
        first = str(e)
    repaired = re.sub(r",\s*([}\]])", r"\1", s)                         # trailing commas
    if '"' not in repaired:
        repaired = repaired.replace("'", '"')                          # {'tool': 'calc'}
    repaired = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", repaired)))
    if repaired != s:
        try:
            return json.loads(repaired, parse_constant=_reject_constant), ""
        except (ValueError, RecursionError):
            pass
    return None, f"not valid JSON ({first[:80]})"


def _normalise(obj) -> tuple[str, dict, str]:
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if not isinstance(obj, dict):
        return "", {}, "a tool call must be a JSON object"
    if isinstance(obj.get("function"), dict):                          # OpenAI style
        obj = obj["function"]
    name = obj.get("tool", obj.get("name", obj.get("tool_name", obj.get("function"))))
    if not isinstance(name, str):
        return "", {}, "the tool call has no tool name"
    name = name.strip()
    if not NAME.match(name):
        return "", {}, "bad tool name"
    args = None
    for k in ("args", "arguments", "parameters", "input"):
        if k in obj:
            args = obj[k]
            break
    if args is None:
        args = {k: v for k, v in obj.items() if k not in ("tool", "name", "tool_name", "function", "type", "id")}
    if isinstance(args, str):
        if len(args) > MAX_JSON:
            return "", {}, "arguments too long"
        a2, err = _loads(args) if args.strip() else ({}, "")
        if err:
            return "", {}, "arguments are not valid JSON"
        args = a2
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "", {}, "args must be an object of named arguments"
    return name, args, ""


_FENCE = re.compile(r"```(?:json|tool|tool_call)?\s*(\{.*?\})\s*```", re.S | re.I)


def _find_call(vis: str) -> tuple[int, str]:
    """(position, json text) of the first tool call candidate, or (-1, "")."""
    m = re.search(r"<tool_call>", vis, re.I)
    if m:
        return m.start(), vis[m.end():]           # scanned string-aware: a "</tool_call>" inside a string is fine
    m = _FENCE.search(vis)
    if m and re.search(r'"(tool|name|function)"\s*:', m.group(1)):
        return m.start(), m.group(1)
    m = re.search(r'\{\s*["\']?(tool|name|function)["\']?\s*:', vis)
    if m:
        return m.start(), vis[m.start():]
    return -1, ""


def parse(reply) -> Parsed:
    if not isinstance(reply, str):
        return Parsed("none")
    text = reply[:MAX_REPLY].replace("\x00", "")
    vis, think, truncated = split_think(text)
    pos_call, body = _find_call(vis)
    am = re.search(r"<answer>(.*?)(?:</answer>|$)", vis, re.S | re.I)
    pm = re.search(r"<plan>(.*?)(?:</plan>|$)", vis, re.S | re.I)
    if am and (pos_call < 0 or am.start() < pos_call):
        return Parsed("answer", text=am.group(1).strip()[:20000], think=think, think_truncated=truncated)
    if pos_call >= 0:
        start = body.find("{")
        if start < 0:
            return Parsed("none", text=vis[:4000], error="the tool call has no JSON object", think=think)
        js, why = _scan_object(body, start)
        if js is None:
            return Parsed("none", text=vis[:4000], error=why, think=think)
        obj, err = _loads(js)
        if err:
            return Parsed("none", text=vis[:4000], error=f"the tool call is {err}", think=think)
        name, args, err = _normalise(obj)
        if err:
            return Parsed("none", text=vis[:4000], error=err, think=think)
        extra = max(0, len(re.findall(r"<tool_call>", vis, re.I)) - 1)
        return Parsed("tool", tool=name, args=args, think=think, think_truncated=truncated, extra_calls=extra)
    if pm:
        return Parsed("plan", text=pm.group(1).strip()[:8000], think=think, think_truncated=truncated)
    return Parsed("none", text=vis[:20000], think=think, think_truncated=truncated)


def format_call(name: str, args: dict) -> str:
    """The canonical form we put back into the conversation (never the raw reply)."""
    return "<tool_call>" + json.dumps({"tool": name, "args": args}, ensure_ascii=False)[:4000] + "</tool_call>"
