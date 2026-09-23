"""A scripted stand-in for the Ollama HTTP API so tests run without a GPU."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, replies=None, models=("fake-model",)):
        self.replies = list(replies or [])
        self.models = list(models)
        self.requests = []
        self.pull_lines = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send({"models": [{"name": m} for m in fake.models]})
                elif self.path == "/api/ps":
                    self._send({"models": []})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if self.path == "/api/pull":
                    lines = fake.pull_lines or [{"status": "success"}]
                    data = b"".join(json.dumps(l).encode() + b"\n" for l in lines)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path != "/api/generate":
                    self._send({"error": "not found"}, 404)
                    return
                r = fake.replies.pop(0) if fake.replies else "default reply"
                if callable(r):
                    r = r(body)
                self._send({"response": r, "prompt_eval_count": len(body["prompt"]) // 4,
                            "eval_count": len(r) // 4, "prompt_eval_duration": 10**8,
                            "eval_duration": 10**8})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
