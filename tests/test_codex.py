"""The paid `codex` worker tier, with a fake codex CLI (no network, no login)."""
import json
import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_halo import BAD, CHECK, GOOD, Base  # noqa: E402
from halo import ledger, llm, tasks  # noqa: E402

FAKE = r'''#!{py}
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_CODEX_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"args": args, "cwd": os.getcwd(), "prompt": prompt}}) + "\n")
mode = os.environ.get("FAKE_CODEX_MODE", "good")
if mode == "fail":
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "usage limit reached"}}}}))
    sys.exit(1)
reply = {good!r} if mode == "good" else {bad!r}
print(json.dumps({{"type": "thread.started", "thread_id": "t"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": reply}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1000,
      "cached_input_tokens": 800, "output_tokens": 40, "reasoning_output_tokens": 10}}}}))
'''


class TestCodexTier(Base):
    def setUp(self):
        super().setUp()
        exe = os.path.join(self.dir, "codex")
        with open(exe, "w") as fh:
            fh.write(FAKE.format(py=sys.executable, good=GOOD, bad=BAD))
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
        self.cfg.codex_bin = exe
        self.log = os.path.join(self.dir, "codex.log")
        os.environ["FAKE_CODEX_LOG"] = self.log
        os.environ["FAKE_CODEX_MODE"] = "good"

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def test_is_cloud(self):
        self.assertTrue(llm.is_cloud("codex"))
        self.assertTrue(llm.is_cloud("codex:gpt-5"))
        self.assertFalse(llm.is_cloud("qwen3:8b"))
        self.assertFalse(llm.is_cloud("codexlike"))

    def test_local_first_then_codex(self):
        self.cfg.fallback_models = ["codex:gpt-5"]
        self.fake.replies = [BAD] * 5
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir,
                       max_iters=2)
        self.assertEqual((r["status"], r["model"], r["attempts"]), ("passed", "codex:gpt-5", 3))
        self.assertEqual(len(self.fake.requests), 2)  # the free model was tried first
        c = self.calls()[0]
        self.assertIn("-m", c["args"])
        self.assertEqual(c["args"][c["args"].index("-m") + 1], "gpt-5")
        # text-only worker: read-only sandbox, empty scratch workspace, never the project dir
        self.assertEqual(c["args"][c["args"].index("-s") + 1], "read-only")
        self.assertNotEqual(os.path.realpath(c["cwd"]), os.path.realpath(self.dir))
        self.assertIn("Do NOT run shell commands", c["prompt"])
        self.assertIn("add two numbers", c["prompt"])

    def test_cloud_tokens_are_not_local(self):
        self.cfg.fallback_models = ["codex"]
        self.fake.replies = [BAD] * 3
        tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir, max_iters=1)
        rec = ledger.read(ledger.LEDGER)[-1]
        self.assertEqual((rec["cloud_in"], rec["cloud_out"], rec["cloud_calls"]), (1000, 50, 1))
        self.assertLess(rec["local_in"], 1000)
        s = ledger.summary(ledger.read(ledger.LEDGER))
        self.assertIn("not free", ledger.format_summary(s))

    def test_codex_failure_is_an_error_not_a_crash(self):
        os.environ["FAKE_CODEX_MODE"] = "fail"
        with self.assertRaises(llm.LocalModelError) as e:
            llm.generate(self.cfg, "hi", model="codex")
        self.assertIn("usage limit", str(e.exception))

    def test_missing_cli(self):
        self.cfg.codex_bin = os.path.join(self.dir, "nope")
        with self.assertRaises(llm.LocalModelError):
            llm.generate(self.cfg, "hi", model="codex")

    def test_ask_via_codex(self):
        r = tasks.ask(self.cfg, "q", model="codex")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
