"""HFF handoff: a fake vendor agent CLI edits files in HALO's scratch copy of a project."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_cli import make_cli  # noqa: E402
from test_halo import Base  # noqa: E402
from halo import handoff, ledger, mcp_server  # noqa: E402

# Round N applies FAKE_ROUNDS[N-1]: {"write": {path: text}, "delete": [paths], "reply": str,
# "touch_real": [path, text]} (the last one edits the REAL project, as a controller might).
FAKE = r'''import json, os, sys
prompt = sys.stdin.read()
log = os.environ["FAKE_AGENT_LOG"]
n = sum(1 for _ in open(log)) if os.path.exists(log) else 0
with open(log, "a") as fh:
    fh.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                         "files": sorted(os.listdir("."))}) + "\n")
rounds = json.loads(os.environ["FAKE_ROUNDS"])
r = rounds[min(n, len(rounds) - 1)]
for path, text in r.get("write", {}).items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write(text)
for path in r.get("delete", []):
    os.remove(path)
if "touch_real" in r:
    open(r["touch_real"][0], "w").write(r["touch_real"][1])
print(json.dumps({"type": "item.completed",
                  "item": {"type": "agent_message", "text": r.get("reply", "done")}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500,
                  "output_tokens": 50}}))
sys.exit(r.get("exit", 0))
'''

CHECK = f'{sys.executable} -c "import sys; sys.path.insert(0, \'src\'); import x; assert x.f() == 2"'
GOOD = "def f():\n    return 2\n"
BAD = "def f():\n    return 1\n"


class TestHandoff(Base):
    def setUp(self):
        super().setUp()
        self.cfg.codex_bin = make_cli(os.path.join(self.dir, "bin"), "codex", FAKE)
        self.proj = os.path.join(self.dir, "proj")
        os.makedirs(os.path.join(self.proj, "src"))
        self.put("src/x.py", BAD)
        self.put("README.md", "readme\n")
        self.put(".env", "API_KEY=secret\n")
        os.makedirs(os.path.join(self.proj, ".git"))
        self.put(".git/HEAD", "ref\n")
        self.log = os.path.join(self.dir, "agent.log")
        self._env = mock.patch.dict(os.environ, {"FAKE_AGENT_LOG": self.log})
        self._env.start()
        self._patches = mock.patch.object(handoff, "PATCH_DIR", os.path.join(self.dir, "patches"))
        self._patches.start()

    def tearDown(self):
        self._patches.stop()
        self._env.stop()
        super().tearDown()

    def put(self, rel, text):
        with open(os.path.join(self.proj, rel), "w") as fh:
            fh.write(text)

    def get(self, rel):
        with open(os.path.join(self.proj, rel)) as fh:
            return fh.read()

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def run_rounds(self, script, **kw):
        os.environ["FAKE_ROUNDS"] = json.dumps(script)
        kw.setdefault("check", CHECK)
        return handoff.handoff(self.cfg, "make f return 2", kw.pop("sector", ["src"]),
                               workdir=self.proj, agent="codex", **kw)

    def test_passes_applies_sector_only_and_hides_secrets(self):
        r = self.run_rounds([{"write": {"src/x.py": GOOD, "README.md": "hacked\n",
                                        "src/new.py": "N = 1\n"}, "reply": "fixed f"}])
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue(r["applied"])
        self.assertEqual(self.get("src/x.py"), GOOD)
        self.assertEqual(self.get("src/new.py"), "N = 1\n")
        self.assertEqual(self.get("README.md"), "readme\n")  # outside the sector: dropped
        self.assertEqual(r["dropped_outside_sector"], ["README.md"])
        self.assertIn("src/x.py: +1 -1", r["diffstat"])
        self.assertIn("src/new.py: +1 -0 (new)", r["diffstat"])
        self.assertEqual(r["agent_summary"], "fixed f")
        call = self.calls()[0]
        self.assertNotIn(".env", call["files"])  # secrets never reach the other vendor
        self.assertNotIn(".git", call["files"])
        self.assertNotEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.proj))
        self.assertIn("workspace-write", call["args"])
        self.assertIn("ONLY inside this sector: src", call["prompt"])
        rec = ledger.read(ledger.LEDGER)[-1]
        self.assertEqual((rec["kind"], rec["status"]), ("handoff", "passed"))
        self.assertEqual(rec["cloud_in"], 500)

    def test_repair_round_gets_check_output(self):
        r = self.run_rounds([{"write": {"src/x.py": "def f():\n    return 3\n"}},
                             {"write": {"src/x.py": GOOD}}])
        self.assertEqual((r["status"], r["rounds"]), ("passed", 2))
        second = self.calls()[1]["prompt"]
        self.assertIn("did not pass the check", second)
        self.assertIn("AssertionError", second)

    def test_failure_keeps_project_and_saves_patch(self):
        r = self.run_rounds([{"write": {"src/x.py": "def f():\n    return 3\n"}}], rounds=2)
        self.assertEqual(r["status"], "failed")
        self.assertIn("after 2 round(s)", r["reason"])
        self.assertFalse(r["applied"])
        self.assertEqual(self.get("src/x.py"), BAD)
        with open(r["patch"]) as fh:
            self.assertIn("+    return 3", fh.read())
        self.assertIn("AssertionError", r["last_check_output"])

    def test_conflict_with_controller_edit_is_not_applied(self):
        real = os.path.join(self.proj, "src", "x.py")
        r = self.run_rounds([{"write": {"src/x.py": GOOD}, "touch_real": [real, "# mine\n"]}])
        self.assertEqual(r["status"], "passed")
        self.assertFalse(r["applied"])
        self.assertEqual(r["conflicts"], ["src/x.py"])
        self.assertEqual(self.get("src/x.py"), "# mine\n")
        self.assertTrue(os.path.exists(r["patch"]))

    def test_no_check_is_done_and_delete_applies(self):
        r = self.run_rounds([{"delete": ["src/x.py"], "write": {"src/y.py": GOOD}}], check="")
        self.assertEqual(r["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.proj, "src", "x.py")))
        self.assertIn("src/x.py: +0 -2 (deleted)", r["diffstat"])

    def test_small_diff_inline_large_diff_omitted(self):
        r = self.run_rounds([{"write": {"src/x.py": GOOD}}])
        self.assertIn("+    return 2", r["diff"])
        big = GOOD + "".join(f"# filler line {i} " + "x" * 60 + "\n" for i in range(400))
        r = self.run_rounds([{"write": {"src/x.py": big}}])
        self.assertNotIn("diff", r)
        self.assertIn("full_diff", r["diff_omitted"])
        r = self.run_rounds([{"write": {"src/x.py": GOOD}}], diff_mode="stat")
        self.assertNotIn("diff", r)

    def test_check_ignores_edits_outside_sector(self):
        # The agent "fixes" the test instead of the code. Only the sector comes back, so the
        # check must judge the sector alone, or a pass here is a failure in the real project.
        os.makedirs(os.path.join(self.proj, "tests"))
        self.put("tests/t.py", "import sys; sys.path.insert(0, 'src'); import x\n"
                               "assert x.f() == 2\n")
        check = f"{sys.executable} tests/t.py"
        r = self.run_rounds([{"write": {"tests/t.py": "pass\n"}},
                             {"write": {"tests/t.py": "pass\n", "src/x.py": GOOD}}],
                            check=check, rounds=2)
        self.assertEqual((r["status"], r["rounds"]), ("passed", 2), r)
        self.assertEqual(r["dropped_outside_sector"], ["tests/t.py"])
        self.assertIn("outside the sector", self.calls()[1]["prompt"])
        self.assertIn("tests/t.py", self.calls()[1]["prompt"])
        self.assertEqual(self.get("tests/t.py").count("assert"), 1)
        self.assertEqual(self.get("src/x.py"), GOOD)

    def test_glob_sector(self):
        r = self.run_rounds([{"write": {"src/x.py": GOOD, "src/x.txt": "t\n"}}],
                            sector=["src/*.py"])
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["dropped_outside_sector"], ["src/x.txt"])

    def test_escalation_and_nothing_changed(self):
        r = self.run_rounds([{"reply": "#ESCALATE: spec contradicts the tests"}])
        self.assertEqual((r["status"], r["reason"]), ("escalated", "spec contradicts the tests"))
        r = self.run_rounds([{"reply": "looked around"}], check="")
        self.assertEqual(r["status"], "failed")
        self.assertIn("changed nothing", r["reason"])

    def test_crash_after_edits_still_checked(self):
        # The CLI dies after writing (e.g. one malformed tool call): its edits get judged.
        r = self.run_rounds([{"write": {"src/x.py": "def f():\n    return 3\n"}, "exit": 1},
                             {"write": {"src/x.py": GOOD}}], rounds=2)
        self.assertEqual((r["status"], r["rounds"]), ("passed", 2), r)
        self.assertIn("did not pass the check", self.calls()[1]["prompt"])
        # A crash before any edit in the sector is still an error, not a wasted round.
        r = self.run_rounds([{"write": {"README.md": "x\n"}, "exit": 1}], rounds=2)
        self.assertEqual(r["status"], "error", r)
        self.assertEqual(r["rounds"], 1)

    def test_bad_requests(self):
        self.assertEqual(handoff.handoff(self.cfg, "t", ["../other"], workdir=self.proj,
                                         agent="codex")["status"], "error")
        self.assertEqual(handoff.handoff(self.cfg, "t", [], workdir=self.proj,
                                         agent="codex")["status"], "error")
        r = handoff.handoff(self.cfg, "t", ["src"], workdir=self.proj, agent="qwen3:8b")
        self.assertIn("not a vendor agent", r["error"])

    def test_mcp_tool_respects_allowed_roots(self):
        r = mcp_server.call_tool("halo_handoff", {"task": "t", "sector": ["src"],
                                                  "workdir": "/"})
        self.assertIn("outside the allowed directories", r["error"])


if __name__ == "__main__":
    unittest.main()
