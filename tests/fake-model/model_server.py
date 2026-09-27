#!/usr/bin/env python3
"""A scripted model server for the harness e2e suite (tests/e2e/). A real agent
CLI talks to it as if it were a model, so the suite runs the CLI's real prompt
assembly, tool layer, permission rules and plugin hooks with no credentials, no
network and no tokens. It speaks both wires the supported CLIs use:

  * OpenAI chat completions (POST /v1/chat/completions) — GitHub Copilot CLI in
    BYOK mode: COPILOT_PROVIDER_BASE_URL=<url>/v1 + COPILOT_OFFLINE=true.
  * Anthropic Messages (POST /v1/messages) — Claude Code: ANTHROPIC_BASE_URL=<url>.

Both streamed (SSE) and plain JSON responses are served.

The "model" makes no decisions: it plays a SCRIPT, one turn per agent request,
so every run is deterministic. What the suite proves is the wiring around the
model, never the model's judgment (that is tests/agent/'s job). Only requests
that offer tools advance the script — a CLI's side requests (a session title, a
summary) get a canned answer, so they can never eat a scripted step.

Script (a JSON list, one entry per model turn):
  {"bash": "<command>"}   call the CLI's shell tool with that command
                          ("bash" on Copilot, "Bash" on Claude Code; override
                          the tool name with "tool")
  {"say": "<text>"}       answer with plain text (ends the agent's turn)
  {"fail": {"status": 429, "type": "rate_limit_error", "message": "..."}}
                          answer with that HTTP error, in the wire's error shape.
                          It sticks: every later agent request fails the same
                          way, as a usage limit does across the CLI's retries.
Once the script is exhausted every further turn answers "done", so a CLI that
asks more than the script planned still terminates.

Every request body is appended to the record file (JSON lines), so a test can
assert on what the CLI actually sent: the system prompt, the user prompt, the
tool list, and the tool results that came back from each scripted call.

Usage (standalone):  model_server.py SCRIPT.json RECORD.jsonl   → prints the base URL
In-process:          start(script, record_path)           → (httpd, base_url)
"""
import http.server
import json
import sys
import threading

MODEL = "fake-model"
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


class _Script:
    """The turns still to play, shared by every request the server handles."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.lock = threading.Lock()
        self.calls = 0

    def next_turn(self):
        with self.lock:
            self.calls += 1
            if self.turns and "fail" in self.turns[0]:
                return self.turns[0], self.calls
            return self.turns.pop(0) if self.turns else {"say": "done"}, self.calls


def _tool_call(turn, call_no, default_tool):
    """(call id, tool name, input) for a scripted shell step."""
    args = {"command": turn["bash"], "description": "scripted step %d" % call_no}
    return "call_%d" % call_no, turn.get("tool", default_tool), args


def _handler(script, record_path):
    record_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        # --- plumbing -------------------------------------------------------

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, events):
            """events: [(event name or None, data object or raw string)]."""
            body = b""
            for name, data in events:
                if name:
                    body += b"event: " + name.encode() + b"\n"
                raw = data if isinstance(data, str) else json.dumps(data)
                body += b"data: " + raw.encode() + b"\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, fail, anthropic):
            status = int(fail.get("status", 500))
            kind = fail.get("type", "api_error")
            message = fail.get("message", "scripted failure")
            if anthropic:
                obj = {"type": "error", "error": {"type": kind, "message": message}}
            else:
                obj = {"error": {"type": kind, "code": kind, "message": message}}
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in fail.get("headers", {}).items():
                self.send_header(name, str(value))
            self.end_headers()
            self.wfile.write(body)

        def _turn(self, req):
            """The next scripted turn — or, for a request that offers no tools
            (a CLI's side request), a canned one that consumes nothing."""
            if not req.get("tools"):
                return {"say": "ok"}, 0
            return script.next_turn()

        # --- routes ---------------------------------------------------------

        def do_GET(self):  # model listing (both wires accept this shape)
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model",
                                                    "type": "model", "display_name": MODEL}]})

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?", 1)[0]
            if path.endswith("/count_tokens"):
                self._json({"input_tokens": 1})
                return
            with record_lock, open(record_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"path": path, "body": req}) + "\n")
            if path.endswith("/messages"):
                self._anthropic(req)
            else:
                self._openai(req)

        # --- OpenAI chat completions (Copilot CLI BYOK) ---------------------

        def _openai(self, req):
            turn, call_no = self._turn(req)
            if "fail" in turn:
                self._error(turn["fail"], anthropic=False)
                return
            if "bash" in turn:
                cid, name, args = _tool_call(turn, call_no, "bash")
                msg = {"role": "assistant", "content": None,
                       "tool_calls": [{"id": cid, "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            else:
                msg, finish = {"role": "assistant", "content": turn["say"]}, "stop"
            head = {"id": "fake-%d" % call_no, "model": MODEL}
            if not req.get("stream"):
                self._json(dict(head, object="chat.completion", usage=USAGE,
                                choices=[{"index": 0, "message": msg, "finish_reason": finish}]))
                return
            delta = dict(msg)
            if "tool_calls" in delta:
                delta["tool_calls"] = [dict(tc, index=i) for i, tc in enumerate(delta["tool_calls"])]
            chunk = dict(head, object="chat.completion.chunk")
            self._sse([
                (None, dict(chunk, choices=[{"index": 0, "delta": delta, "finish_reason": None}])),
                (None, dict(chunk, usage=USAGE,
                            choices=[{"index": 0, "delta": {}, "finish_reason": finish}])),
                (None, "[DONE]"),
            ])

        # --- Anthropic Messages (Claude Code) -------------------------------

        def _anthropic(self, req):
            turn, call_no = self._turn(req)
            if "fail" in turn:
                self._error(turn["fail"], anthropic=True)
                return
            if "bash" in turn:
                cid, name, args = _tool_call(turn, call_no, "Bash")
                block = {"type": "tool_use", "id": "toolu_%s" % cid, "name": name, "input": args}
                stop = "tool_use"
            else:
                block, stop = {"type": "text", "text": turn["say"]}, "end_turn"
            usage = {"input_tokens": 1, "output_tokens": 1}
            message = {"id": "msg_fake_%d" % call_no, "type": "message", "role": "assistant",
                       "model": req.get("model", MODEL), "stop_sequence": None}
            if not req.get("stream"):
                self._json(dict(message, content=[block], stop_reason=stop, usage=usage))
                return
            if block["type"] == "tool_use":
                start = dict(block, input={})
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            self._sse([
                ("message_start", {"type": "message_start",
                                   "message": dict(message, content=[], stop_reason=None, usage=usage)}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": start}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": stop, "stop_sequence": None},
                                   "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"}),
            ])

    return Handler


def start(turns, record_path):
    """Serve `turns` on an ephemeral localhost port from a daemon thread.
    Returns (httpd, base_url) — the server root: Claude Code takes it as is,
    Copilot CLI wants it with /v1. The caller shuts the server down."""
    open(record_path, "a", encoding="utf-8").close()
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(_Script(turns), record_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_port


def read_record(record_path):
    """Every request the CLI sent, in order, as parsed bodies."""
    with open(record_path, encoding="utf-8") as f:
        return [json.loads(line)["body"] for line in f if line.strip()]


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        _turns = json.load(f)
    _httpd, _url = start(_turns, sys.argv[2])
    print(_url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        _httpd.shutdown()
