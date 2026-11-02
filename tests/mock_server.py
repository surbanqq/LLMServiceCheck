"""Local fake service for harness verification only. NOT a model or performance reference."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import re


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        auth = self.headers.get("Authorization", "")
        if "deliberately-invalid" in auth:
            self.reply({"error": {"type": "authentication_error", "code": "invalid_api_key"}}, 401)
            return
        if body.get("max_tokens", 1) < 0 or body.get("temperature", 0) < 0:
            self.reply({"error": {"type": "invalid_request_error", "code": "invalid_parameter"}}, 400)
            return
        text = " ".join(m.get("content", "") for m in body["messages"])
        target = re.search(r"MOCK_TOKENS=(\d+)", text)
        prompt_tokens = int(target.group(1)) if target else 50
        completion_tokens = 256 if body.get("max_tokens", 512) >= 256 else min(32, body.get("max_tokens", 32))
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens,
                 "prompt_tokens_details": {"cached_tokens": 0}}
        if "MISSING_USAGE" in text:
            usage = None
        if "CACHE_HIT" in text and usage is not None:
            usage["prompt_tokens_details"]["cached_tokens"] = int(prompt_tokens * .9)
        if "SHORT_OUTPUT" in text and usage is not None:
            usage["completion_tokens"] = 1
        if "MALFORMED_USAGE" in text:
            usage = ["invalid", "usage"]
        tool = None
        if body.get("tools"):
            cities = ["Beijing", "Shanghai"] if body.get("parallel_tool_calls") else ["Beijing"]
            tool = [{"id": f"call_{i}", "type": "function",
                     "function": {"name": "get_weather", "arguments": json.dumps({"city": city})}}
                    for i, city in enumerate(cities)]
        content = '{"ok":true}' if body.get("response_format") else "OK mock generated answer."
        if "REASONING" in text:
            content = "Mock reasoning answer"
        if not body.get("stream"):
            self.reply({"id": "mock_completion", "object": "chat.completion",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                                     "tool_calls": tool or []},
                                     "finish_reason": "tool_calls" if tool else "stop"}],
                        "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-request-id", "mock-request-id")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        def event(obj):
            # Alternate LF/CRLF; harmless comments and role-only chunks precede generation.
            raw = "data: " + json.dumps(obj, ensure_ascii=False) + "\r\n\r\n"
            self.wfile.write(raw.encode("utf-8"))
            self.wfile.flush()
        try:
            if "SILENCE" in text:
                time.sleep(.25)
            self.wfile.write(b": heartbeat\n\n")
            event({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant"}}]})
            if "STALL" in text:
                for _ in range(100):
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(.02)
                return
            time.sleep(.004)
            if "STREAM_ERROR" in text:
                event({"error": {"message": "mock stream error"}})
                return
            if tool:
                for t in tool:
                    event({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {
                        "tool_calls": [{"index": 0, **t}]}}]})
            else:
                field = "reasoning_content" if "REASONING" in text else "content"
                for part in [content[:3], content[3:]]:
                    event({"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {field: part}}]})
                    time.sleep(.004)
            event({"object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]})
            if usage is not None:
                event({"object": "chat.completion.chunk", "choices": [], "usage": usage})
            if "NO_DONE" not in text:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def reply(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("x-request-id", "mock-request-id")
        self.end_headers()
        self.wfile.write(raw)


def start():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("Mock only: http://127.0.0.1:8765/v1", flush=True)
    server.serve_forever()
