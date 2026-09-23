import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_ollama import FakeOllama  # noqa: E402
from halo import config, ledger, mcp_server, router, tasks  # noqa: E402

PY = sys.executable


class Base(unittest.TestCase):
    replies: list = []

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.fake = FakeOllama(self.replies)
        self.cfg = config.Config(ollama_url=self.fake.url, model="fake-model", max_iters=3)
        self._ledger = mock.patch.object(ledger, "LEDGER", os.path.join(self.dir, "l.jsonl"))
        self._ledger.start()

    def tearDown(self):
        self._ledger.stop()
        self.fake.close()
        self.tmp.cleanup()

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p


class TestAsk(Base):
    def test_ok_and_ledger(self):
        self.fake.replies = ["Paris"]
        r = tasks.ask(self.cfg, "capital of France?")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["answer"], "Paris")
        self.assertFalse(self.fake.requests[0]["think"], "thinking must be disabled")
        recs = ledger.read(ledger.LEDGER)
        self.assertEqual(recs[0]["kind"], "ask")

    def test_escalate(self):
        self.fake.replies = ["#ESCALATE: needs repository context"]
        r = tasks.ask(self.cfg, "why does the build fail?")
        self.assertEqual(r["status"], "escalated")
        self.assertEqual(r["reason"], "needs repository context")

    def test_ollama_down(self):
        cfg = config.Config(ollama_url="http://127.0.0.1:1", model="x")
        self.assertEqual(tasks.ask(cfg, "hi")["status"], "error")


class TestDigest(Base):
    def test_single_chunk(self):
        self.fake.replies = ["ERROR at line 3: disk full"]
        p = self.write("a.log", "ok\nok\nERROR disk full\n")
        r = tasks.digest(self.cfg, "what failed?", [p])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["chunks"], 1)
        self.assertIn("disk full", self.fake.requests[0]["prompt"])

    def test_map_reduce_on_large_input(self):
        self.cfg.num_ctx = 2000  # budget = (2000-1500)*2.5 = 1250 chars per chunk
        def reply(body):
            p = body["prompt"]
            if p.startswith("This is part"):
                return "found NEEDLE=42" if "NEEDLE=42" in p else "(nothing)"
            return "NEEDLE is 42"
        self.fake.replies = [reply] * 50
        lines = [f"filler line {i}\n" for i in range(400)]
        lines[250] = "NEEDLE=42\n"
        p = self.write("big.log", "".join(lines))
        r = tasks.digest(self.cfg, "value of NEEDLE?", [p])
        self.assertEqual(r["status"], "ok")
        self.assertGreater(r["chunks"], 3)
        self.assertEqual(r["answer"], "NEEDLE is 42")
        # the reduce step must only see the relevant note, not every chunk
        self.assertIn("found NEEDLE=42", self.fake.requests[-1]["prompt"])
        self.assertNotIn("(nothing)", self.fake.requests[-1]["prompt"])

    def test_no_line_is_lost_when_chunking(self):
        text = "".join(f"line {i}\n" for i in range(1000)) + "x" * 5000 + "\n"
        self.assertEqual("".join(tasks._chunks(text, 700)), text)
        self.assertTrue(all(len(c) <= 700 for c in tasks._chunks(text, 700)))

    def test_missing_file(self):
        r = tasks.digest(self.cfg, "q", [os.path.join(self.dir, "nope.txt")])
        self.assertEqual(r["status"], "error")


GOOD = "```python\ndef add(a, b):\n    return a + b\n```"
BAD = "```python\ndef add(a, b):\n    return a - b\n```"
CHECK = f"{PY} -c \"from mod import add; assert add(2, 3) == 5\""


class TestCode(Base):
    def test_passes_first_try(self):
        self.fake.replies = [GOOD]
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["attempts"], 1)
        self.assertIn("+2", r["diffstat"])
        self.assertNotIn("diff", r)  # diffstat only by default

    def test_retries_with_error_feedback(self):
        self.fake.replies = [BAD, GOOD]
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["attempts"], 2)
        second = self.fake.requests[1]["prompt"]
        self.assertIn("FAILED", second)
        self.assertIn("AssertionError", second)

    def test_failure_restores_original(self):
        self.write("mod.py", "# original\n")
        self.fake.replies = [BAD, BAD, BAD]
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "failed")
        with open(os.path.join(self.dir, "mod.py")) as fh:
            self.assertEqual(fh.read(), "# original\n")
        self.assertIn("AssertionError", r["last_check_output"])
        self.assertEqual(len(ledger.read(ledger.LEDGER)), 1)

    def test_failure_removes_new_file(self):
        self.fake.replies = [BAD] * 3
        tasks.code(self.cfg, "add", "new.py", f"{PY} -c 'import new; assert 0'", workdir=self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "new.py")))

    def test_escalation_stops_loop(self):
        self.fake.replies = ["#ESCALATE: spec references unknown API"]
        r = tasks.code(self.cfg, "use frobnicate()", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "escalated")
        self.assertEqual(len(self.fake.requests), 1)

    def test_refuses_path_outside_workdir(self):
        r = tasks.code(self.cfg, "x", "../evil.py", "true", workdir=self.dir)
        self.assertEqual(r["status"], "error")
        self.assertEqual(self.fake.requests, [])

    def test_requires_check(self):
        self.assertEqual(tasks.code(self.cfg, "x", "a.py", " ", workdir=self.dir)["status"],
                         "error")

    def test_extract_code(self):
        self.assertEqual(tasks.extract_code("text\n```py\nA\n```\n```\nBBB\n```"), "BBB\n")
        self.assertIsNone(tasks.extract_code("Here is the code you asked for"))


class TestLedger(Base):
    def test_only_successes_count_as_saved(self):
        u = type("U", (), {"prompt_tokens": 10, "output_tokens": 5, "calls": 1})()
        ledger.record("digest", "ok", "m", u, direct_est=1000, returned_est=50, seconds=1)
        ledger.record("code", "failed", "m", u, direct_est=900, returned_est=100, seconds=1)
        s = ledger.summary(ledger.read(ledger.LEDGER))
        self.assertEqual(s["frontier_saved_est"], 950)
        self.assertEqual(s["total"]["tasks"], 2)
        self.assertIn("Frontier tokens saved", ledger.format_summary(s))


class TestRouter(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(router.route("translate 'hello' to Thai")[0], "local")
        self.assertEqual(router.route("fix the bug in parser")[0], "frontier")
        self.assertEqual(router.route("what does ./run.sh do")[0], "frontier")
        self.assertEqual(router.route("ทำไมถึงช้า")[0], "frontier")
        self.assertEqual(router.route("summarize", has_input=True)[0], "local")
        # substring must not trigger: "prefix" contains "fix"
        self.assertEqual(router.route("what is a prefix tree")[0], "local")


class TestMCP(Base):
    def rpc(self, method, params=None, mid=1):
        return mcp_server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                                  "params": params or {}})

    def test_handshake_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0",
                                             "method": "notifications/initialized"}))
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"halo_digest", "halo_code", "halo_ask", "halo_stats"})

    def test_call_digest(self):
        self.fake.replies = ["answer"]
        p = self.write("x.txt", "hello")
        with mock.patch.object(config, "load", return_value=self.cfg):
            r = self.rpc("tools/call", {"name": "halo_digest",
                                        "arguments": {"question": "q", "paths": [p]}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["answer"], "answer")
        self.assertFalse(r["result"]["isError"])

    def test_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        r = self.rpc("tools/call", {"name": "halo_nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_stdio_loop(self):
        lines = "\n".join(json.dumps(m) for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]) + "\nnot json\n"
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(lines)), \
                mock.patch.object(sys, "stdout", out):
            mcp_server.main()
        resp = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in resp], [1, 2, None])
        self.assertEqual(resp[2]["error"]["code"], -32700)


class TestConfig(unittest.TestCase):
    def test_env_overrides(self):
        env = {"HALO_MODEL": "m2", "HALO_CTX": "4096", "OLLAMA_HOST": "10.0.0.5:11434"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(config, "CONFIG_PATH", "/nonexistent/halo.json"):
            c = config.load()
        self.assertEqual((c.model, c.num_ctx, c.ollama_url), ("m2", 4096, "http://10.0.0.5:11434"))


if __name__ == "__main__":
    unittest.main()
