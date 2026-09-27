#!/usr/bin/env python3
"""A scripted, OpenAI-compatible model server for the harness e2e suite
(tests/e2e/). A real agent CLI — GitHub Copilot CLI in BYOK mode
(COPILOT_PROVIDER_BASE_URL + COPILOT_OFFLINE=true) — talks to it as if it were a
model, so the suite runs the CLI's real prompt assembly, tool layer, permission
rules and plugin hooks with no credentials, no network and no tokens.

The "model" makes no decisions: it plays a SCRIPT, one turn per chat-completion
request, so every run is deterministic. What the suite proves is the wiring
around the model, never the model's judgment (that is tests/agent/'s job).

Script (a JSON list, one entry per model turn):
  {"bash": "<command>"}   call the CLI's shell tool with that command
  {"say": "<text>"}       answer with plain text (ends the agent's turn)
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


class _Script:
    """The turns still to play, shared by every request the server handles."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.lock = threading.Lock()
        self.calls = 0

    def next_turn(self):
        with self.lock:
            self.calls += 1
            return self.turns.pop(0) if self.turns else {"say": "done"}, self.calls


def _message(turn, call_no):
    """The assistant message and finish reason one script turn becomes."""
    if "bash" in turn:
        args = {"command": turn["bash"], "description": "scripted step %d" % call_no}
        return ({"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_%d" % call_no, "type": "function",
                                 "function": {"name": turn.get("tool", "bash"),
                                              "arguments": json.dumps(args)}}]},
                "tool_calls")
    return {"role": "assistant", "content": turn["say"]}, "stop"


def _handler(script, record_path):
    record_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # model listing
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            with record_lock, open(record_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"path": self.path, "body": req}) + "\n")
            turn, call_no = script.next_turn()
            msg, finish = _message(turn, call_no)
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if not req.get("stream"):
                self._json({"id": "fake-%d" % call_no, "object": "chat.completion",
                            "model": MODEL, "usage": usage,
                            "choices": [{"index": 0, "message": msg,
                                         "finish_reason": finish}]})
                return
            delta = dict(msg)
            if "tool_calls" in delta:
                delta["tool_calls"] = [dict(tc, index=i)
                                       for i, tc in enumerate(delta["tool_calls"])]
            chunks = [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": usage},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                chunk.update({"id": "fake-%d" % call_no,
                              "object": "chat.completion.chunk", "model": MODEL})
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")

    return Handler


def start(turns, record_path):
    """Serve `turns` on an ephemeral localhost port from a daemon thread.
    Returns (httpd, base_url); the caller shuts it down."""
    open(record_path, "a", encoding="utf-8").close()
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(_Script(turns), record_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/v1" % httpd.server_port


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
