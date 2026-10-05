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
from halo import config, ledger, llm, mcp_server, router, tasks  # noqa: E402

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
        self._config = mock.patch.object(config, "CONFIG_PATH", os.path.join(self.dir, "c.json"))
        self._config.start()

    def tearDown(self):
        self._config.stop()
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


class TestTransientOllama(Base):
    """A model swap by another client makes Ollama answer 200 with an empty, unfinished
    object. That is a retry, not the model writing an empty file."""
    UNFINISHED = {"model": "", "created_at": "0001-01-01T00:00:00Z", "response": "",
                  "done": False}

    def setUp(self):
        super().setUp()
        self._waits = mock.patch.object(llm, "RETRY_WAITS", (0, 0))
        self._waits.start()

    def tearDown(self):
        self._waits.stop()
        super().tearDown()

    def test_unfinished_reply_is_retried(self):
        self.fake.replies = [self.UNFINISHED, self.UNFINISHED, GOOD]
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir,
                       max_iters=1)
        self.assertEqual((r["status"], r["attempts"]), ("passed", 1))
        self.assertEqual(len(self.fake.requests), 3)

    def test_persistent_unfinished_reply_is_an_error(self):
        self.fake.replies = [self.UNFINISHED] * 3
        with self.assertRaises(llm.LocalModelError) as e:
            llm.generate(self.cfg, "hi")
        self.assertIn("unfinished", str(e.exception))


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
            if p.startswith("<material part"):
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

    def test_compact_log(self):
        log = "".join(f"Sep 23 10:{i % 60:02d}:01 app[{100 + i}]: GET /health 200 in {i}ms\n"
                      for i in range(500))
        log += "Sep 23 11:00:00 app[9]: KeyError: 'llamacpp'\n"
        self.assertTrue(tasks.looks_like_log(log))
        small, n, kept, sig = tasks.compact_log(log)
        self.assertEqual((n, kept), (501, 2))
        self.assertIn("[500x, first: Sep 23 10:00:01, last: Sep 23 10:19:01]", small)
        self.assertEqual(sig[0]["count"], 1); self.assertIn("KeyError", sig[0]["line"])
        self.assertIn("KeyError: 'llamacpp'", small)  # the rare line survives verbatim
        self.fake.replies = ["one KeyError"]
        p = self.write("app.log", log)
        r = tasks.digest(self.cfg, "errors?", [p])
        self.assertIn("compacted", r)
        self.assertIn("[500x, first:", self.fake.requests[0]["prompt"])
        self.assertEqual(r["signals"][0]["count"], 1)

    def test_signals_skip_traceback_frames(self):
        log = ("Sep 23 10:00:01 host app[1]: Traceback (most recent call last):\n"
               'Sep 23 10:00:01 host app[1]:   File "/x/_exception_handler.py", line 5, in f\n'
               "Sep 23 10:00:01 host app[1]:     raise_error(x)\n"
               "Sep 23 10:00:01 host app[1]: KeyError: 'k'\n") * 3
        _, _, _, sig = tasks.compact_log(log)
        lines = [g["line"] for g in sig]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("KeyError" in l for l in lines))
        self.assertFalse(any("File" in l for l in lines))

    def test_csv_is_not_compacted(self):
        csv = "".join(f"{i},{i * 2},{i % 7}\n" for i in range(2000))
        self.assertFalse(tasks.looks_like_log(csv))
        self.fake.replies = ["x"] * 50
        r = tasks.digest(self.cfg, "sum of col 2?", text=csv)
        self.assertNotIn("compacted", r)

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
        self.fake.replies = [BAD] * 4  # 3 attempts + 1 re-roll of the unchanged repair
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

    def test_cascade_to_fallback_model(self):
        self.cfg.fallback_models = ["big-model"]
        self.fake.replies = [lambda b: GOOD if b["model"] == "big-model" else BAD] * 10
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir, max_iters=2)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["model"], "big-model")
        self.assertEqual(r["attempts"], 3)
        self.assertEqual([q["model"] for q in self.fake.requests],
                         ["fake-model", "fake-model", "fake-model", "big-model"])
        # the fallback model starts fresh, not from the first model's broken draft
        self.assertNotIn("FAILED", self.fake.requests[2]["prompt"])

    def test_best_draft_is_saved(self):
        two_fail = "```python\ndef add(a, b):\n    return 0\n```"
        check = (f"{PY} -c \"import sys, mod; f = (mod.add(2, 3) != 5) + (mod.add(1, 1) != 2); "
                 f"print(f'failures={{f}}'); sys.exit(1 if f else 0)\"")
        one_fail = "```python\ndef add(a, b):\n    return 2\n```"
        self.fake.replies = [two_fail, one_fail, two_fail]
        r = tasks.code(self.cfg, "add", "mod.py", check, workdir=self.dir)
        self.assertEqual(r["status"], "failed")
        self.assertIn("halo-draft", r["draft"])
        with open(os.path.join(self.dir, "mod.py.halo-draft")) as fh:
            self.assertIn("return 2", fh.read())  # best (fewest failures), not the last
        self.assertFalse(os.path.exists(os.path.join(self.dir, "mod.py")))

    def test_failure_score(self):
        self.assertEqual(tasks.failure_score("FAILED (failures=1, errors=2)"), 3)
        self.assertEqual(tasks.failure_score("=== 2 failed, 5 passed ==="), 2)
        self.assertGreater(tasks.failure_score("SyntaxError: invalid syntax"), 1000)

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
        self.assertEqual(tasks.extract_code("Cause: off by one\nx = 1"), "x = 1\n")

    def test_extract_code_rejects_lone_file_name_and_cut_off_block(self):
        # seen in the long-file bench: a restart answered with just "cron.py" became the file
        self.assertIsNone(tasks.extract_code("cron.py"))
        self.assertIsNone(tasks.extract_code("Cause: x\n```python\ndef f():\n    return"))

    def test_extract_code_keeps_backticks_inside_the_file(self):
        # seen in the mdhtml bench: `s.startswith("```")` closed the block, so every Markdown
        # renderer the model wrote was cut at its first fence check
        src = ('def is_fence(s):\n    return s.startswith("```")\n\n'
               'DOC = """\n```\nnot a closing fence: shorter than the opener\n```\n"""\n')
        self.assertEqual(tasks.extract_code("Cause: x\n````python\n" + src + "````\n"), src)
        self.assertEqual(tasks.extract_code("```python\n" + src.split("\n\n")[0] + "\n```"),
                         'def is_fence(s):\n    return s.startswith("```")\n')

    def test_code_ctx_grows_for_long_files_within_cap(self):
        self.assertEqual(tasks.code_ctx(self.cfg, 2000, 0), self.cfg.num_ctx)
        self.assertEqual(tasks.code_ctx(self.cfg, 12000, 5000), 16384)
        self.cfg.max_ctx = 12000
        self.assertEqual(tasks.code_ctx(self.cfg, 90000, 90000), 12000)

    def test_cut_off_reply_is_retried_with_bigger_context(self):
        cut = {"response": "```python\ndef add(a, b):\n    ret", "done": True,
               "done_reason": "length", "prompt_eval_count": 10, "eval_count": 10}
        self.fake.replies = [cut, GOOD]
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual((r["status"], r["attempts"]), ("passed", 1))
        ctxs = [q["options"]["num_ctx"] for q in self.fake.requests]
        self.assertEqual(ctxs, [self.cfg.num_ctx, self.cfg.num_ctx * 2])

    def test_cut_off_at_cap_is_not_run_as_a_file(self):
        self.cfg.max_ctx = self.cfg.num_ctx
        cut = {"response": "x = (", "done": True, "done_reason": "length"}
        self.fake.replies = [cut, GOOD]
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual((r["status"], r["attempts"]), ("passed", 2))
        self.assertIn("cut off", self.fake.requests[1]["prompt"])

    def test_repair_asks_for_cause_then_restarts_when_stuck(self):
        self.write("mod.py", "# original\n")
        self.fake.replies = [BAD, "Cause: sign\n" + BAD, GOOD]
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["attempts"], 2)  # the unchanged repair did not use up an attempt
        second, third = (q["prompt"] for q in self.fake.requests[1:])
        self.assertIn("Cause:", second)
        self.assertIn("return a - b", second)  # repair is anchored on the draft
        self.assertIn("DIFFERENT approach", third)  # same file again -> start over
        self.assertNotIn("return a - b", third)
        self.assertIn("# original", third)

    def test_restart_after_repair_without_progress(self):
        worse = "```python\ndef add(a, b):\n    return a * b\n```"
        self.fake.replies = [BAD, worse, GOOD]
        r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir)
        self.assertEqual(r["status"], "passed")
        self.assertIn("DIFFERENT approach", self.fake.requests[2]["prompt"])

    def test_feedback_keeps_every_failure(self):
        block = "Traceback (most recent call last):\n" + "  noise\n" * 200
        out = "".join(f"{'=' * 70}\nFAIL: test_{i} (t.T.test_{i})\n{'-' * 70}\n{block}"
                      f"AssertionError: boom{i}\n\n" for i in range(3))
        out += f"{'-' * 70}\nRan 3 tests in 0.1s\n\nFAILED (failures=3)\n"
        fb = tasks.check_feedback(out, "mod.py", "")
        for i in range(3):
            self.assertIn(f"test_{i}", fb)
            self.assertIn(f"boom{i}", fb)
        self.assertIn("failures=3", fb)
        self.assertLess(len(fb), 4000)

    def test_feedback_points_at_the_target_line(self):
        path = os.path.join(self.dir, "mod.py")
        out = f'Traceback:\n  File "{path}", line 2, in add\nZeroDivisionError: x\n'
        fb = tasks.check_feedback(out, path, "def add(a, b):\n    return a / 0\n")
        self.assertIn(">>> your line 2: return a / 0", fb)


class TestLedger(Base):
    def test_only_successes_count_as_saved(self):
        u = type("U", (), {"prompt_tokens": 10, "output_tokens": 5, "calls": 1})()
        ledger.record("digest", "ok", "m", u, seconds=1, chars_per_token=1.0,
                      frontier={"direct_in": 1000, "halo_in": 40, "halo_out": 2})
        ledger.record("code", "failed", "m", u, seconds=1, chars_per_token=1.0,
                      frontier={"direct_out": 900, "halo_in": 100})
        s = ledger.summary(ledger.read(ledger.LEDGER))
        self.assertEqual(s["frontier_saved_est"], 1000 - (40 + 5 * 2))
        self.assertEqual(s["total"]["tasks"], 2)
        self.assertIn("upper bound", ledger.format_summary(s))


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
        self.assertEqual(names, {"halo_digest", "halo_code", "halo_ask", "halo_council", "halo_stats"})

    def test_check_claims(self):
        raw = ("===== a.log =====\nSep 23 10:00:01 svc[1]: started\n"
               "Sep 23 10:05:00 svc[1]: KeyError: 'llamacpp'\nSep 23 11:00:02 svc[1]: started\n")
        c = tasks.check_claims('- last restart 11:00:02 ("svc[1]: started")\n'
                               '- "KeyError: \'llamacpp\'" at 10:05:00\n- crash at 12:34:56', raw)
        got = {v["claim"]: v for v in c["verified"]}
        self.assertEqual(got["svc[1]: started"]["where"], "a.log:3")  # last hit, not first
        self.assertEqual(got["svc[1]: started"]["hits"], 2)
        self.assertEqual(got["10:05:00"]["where"], "a.log:2")
        self.assertEqual(c["not_found"], ["12:34:56"])
        self.assertEqual(tasks.check_claims("no claims here", raw), {})

    def test_check_claims_borrowed_timestamp(self):
        # Seen end-to-end: the restart was given the timestamp of the NEXT line.
        raw = ("Sep 17 14:10:30 host systemd[1]: Started abi-brain-api.service - Abi Brain API\n"
               "Sep 17 14:10:38 host python3[9]: INFO: Uvicorn running on http://x:8801\n")
        c = tasks.check_claims("- systemd[1]: Started abi-brain-api.service - Abi Brain API "
                               "last at Sep 17 14:10:38", raw)
        self.assertEqual(c["verified"], [])
        self.assertIn("line 1", c["not_found"][0])
        self.assertIn("14:10:30", c["not_found"][0])

    def test_call_digest(self):
        self.fake.replies = ["answer"]
        p = self.write("x.txt", "hello")
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch("os.getcwd", return_value=self.dir):
            r = self.rpc("tools/call", {"name": "halo_digest",
                                        "arguments": {"question": "q", "paths": [p]}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["answer"], "answer")
        self.assertFalse(r["result"]["isError"])

    def test_paths_confined_to_project(self):
        self.cfg.allowed_roots = []
        outside = self.write("secret.txt", "token=abc")
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch("os.getcwd", return_value=os.path.join(self.dir, "proj")):
            os.makedirs(os.path.join(self.dir, "proj"), exist_ok=True)
            r = self.rpc("tools/call", {"name": "halo_digest",
                                        "arguments": {"question": "q", "paths": [outside]}})
            payload = json.loads(r["result"]["content"][0]["text"])
            self.assertEqual(payload["status"], "error")
            self.assertIn("outside the allowed", payload["error"])
            r = self.rpc("tools/call", {"name": "halo_code", "arguments": {
                "spec": "x", "target": "a.py", "check": "true", "workdir": "/"}})
            self.assertTrue(r["result"]["isError"])
        self.assertEqual(self.fake.requests, [])

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
