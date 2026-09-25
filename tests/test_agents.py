"""Any agent can be in charge (Claude Code / Codex / Gemini) and any vendor CLI can be a paid
worker. Fake CLIs on PATH record their argv, stdin and environment."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_cli import make_cli  # noqa: E402
from test_halo import BAD, CHECK, GOOD, Base  # noqa: E402
from test_modes import Env  # noqa: E402
from halo import config, integration, ledger, llm, mcp_server, tasks  # noqa: E402

FAKE = r'''import json, os, sys
name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
# `mcp add/remove` get no input: reading an inherited stdin could block forever
stdin = "" if sys.argv[1:2] == ["mcp"] or sys.stdin.isatty() else sys.stdin.read()
with open(os.environ["FAKE_CLI_LOG"], "a") as fh:
    fh.write(json.dumps({{"cli": name, "args": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
                         "worker": os.environ.get("HALO_WORKER")}}) + "\n")
if sys.argv[1:2] == ["mcp"]:
    sys.exit(0)
reply = os.environ.get("FAKE_REPLY", {good!r})
if os.environ.get("FAKE_FAIL") in ("1", name):
    if name == "claude":
        print(json.dumps({{"type": "result", "is_error": True, "result": "rate limited"}}))
    elif name == "gemini":
        print(json.dumps({{"error": {{"type": "quota", "message": "quota exceeded"}}}}))
    sys.exit(1)
if name == "claude":
    print(json.dumps({{"type": "result", "is_error": False, "result": reply,
                      "usage": {{"input_tokens": 700, "cache_read_input_tokens": 100,
                                "output_tokens": 30}}}}))
elif name == "gemini":
    print("Loaded cached credentials.")
    print(json.dumps({{"response": reply, "stats": {{"models": {{"gemini-2.5-flash": {{
        "tokens": {{"prompt": 900, "candidates": 20, "thoughts": 5, "total": 925}}}}}}}}}}))
'''


def make_bin(d: str, names=("claude", "codex", "gemini")) -> str:
    b = os.path.join(d, "bin")
    for n in names:
        make_cli(b, n, FAKE.format(good=GOOD))
    return b


def calls(log: str) -> list:
    with open(log) as fh:
        return [json.loads(l) for l in fh]


class TestVendorWorkers(Base):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.dir, "cli.log")
        self._env = mock.patch.dict(os.environ, {
            "PATH": make_bin(self.dir) + os.pathsep + os.environ["PATH"],
            "FAKE_CLI_LOG": self.log})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def test_claude_worker(self):
        self.cfg.fallback_models = ["claude:haiku"]
        self.fake.replies = [BAD]
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir,
                       max_iters=1)
        self.assertEqual((r["status"], r["model"]), ("passed", "claude:haiku"))
        c = calls(self.log)[0]
        # text only: no tools, no MCP servers, no saved session, model passed through
        for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, c["args"])
        self.assertEqual(c["args"][c["args"].index("--tools") + 1], "")
        self.assertEqual(c["args"][c["args"].index("--model") + 1], "haiku")
        self.assertEqual(c["worker"], "1")
        self.assertIn("add two numbers", c["stdin"])
        rec = ledger.read(ledger.LEDGER)[-1]
        self.assertEqual((rec["cloud_in"], rec["cloud_out"]), (800, 30))

    def test_gemini_worker(self):
        out = llm.generate(self.cfg, "hi", model="gemini:gemini-2.5-flash",
                           usage=(u := llm.Usage()))
        self.assertIn("return a + b", out)
        self.assertEqual((u.cloud_in, u.cloud_out, u.cloud_calls), (900, 25, 1))
        c = calls(self.log)[0]
        self.assertEqual(c["args"][c["args"].index("--approval-mode") + 1], "plan")
        self.assertIn("--allowed-mcp-server-names", c["args"])
        self.assertEqual(c["args"][c["args"].index("-m") + 1], "gemini-2.5-flash")
        self.assertNotEqual(os.path.realpath(c["cwd"]), os.path.realpath(self.dir))

    def test_worker_errors_are_errors(self):
        os.environ["FAKE_FAIL"] = "1"
        try:
            for m, msg in (("claude", "rate limited"), ("gemini", "quota exceeded")):
                with self.assertRaises(llm.LocalModelError) as e:
                    llm.generate(self.cfg, "hi", model=m)
                self.assertIn(msg, str(e.exception))
        finally:
            del os.environ["FAKE_FAIL"]

    def test_chain_of_vendors(self):
        # local fails -> first paid worker errors -> second one passes
        self.cfg.fallback_models = ["gemini", "claude"]
        self.fake.replies = [BAD]
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "gemini"}):
            r = tasks.code(self.cfg, "add", "mod.py", CHECK, workdir=self.dir, max_iters=1)
        self.assertEqual((r["status"], r["model"]), ("passed", "claude"))

    def test_mcp_offers_nothing_inside_a_worker(self):
        with mock.patch.dict(os.environ, {"HALO_WORKER": "1"}):
            r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(r["result"]["tools"], [])
            out = mcp_server.call_tool("halo_ask", {"question": "x"})
            self.assertEqual(out["status"], "off")


class TestAgentChoice(Env):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.d, "cli.log")
        extra = {"PATH": make_bin(self.d) + os.pathsep + os.environ["PATH"],
                 "FAKE_CLI_LOG": self.log,
                 "HALO_CODEX_DIR": os.path.join(self.d, "codex"),
                 "HALO_GEMINI_DIR": os.path.join(self.d, "gemini")}
        self._env = mock.patch.dict(os.environ, extra)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def rules(self, agent):
        p = integration.AGENTS[agent].rules_path()
        return open(p).read() if os.path.exists(p) else ""

    def test_detect(self):
        self.assertEqual(integration.detect(), ["claude", "codex", "gemini"])

    def test_install_codex_and_gemini_only(self):
        integration.install(["codex", "gemini"])
        self.assertIn(integration.BEGIN, self.rules("codex"))
        self.assertIn(integration.BEGIN, self.rules("gemini"))
        self.assertEqual(self.rules("claude"), "")  # not chosen: untouched
        self.assertEqual(config.load().agents, ["codex", "gemini"])
        adds = [(c["cli"], c["args"]) for c in calls(self.log) if "add" in c["args"]]
        self.assertEqual([a[0] for a in adds], ["codex", "gemini"])
        self.assertEqual(adds[0][1][:3], ["mcp", "add", "halo"])
        self.assertEqual(adds[1][1][:5], ["mcp", "add", "--scope", "user", "halo"])

    def test_codex_tools_are_pre_approved(self):
        os.makedirs(os.path.join(self.d, "codex"))
        cfg = os.path.join(self.d, "codex", "config.toml")
        with open(cfg, "w") as fh:  # what `codex mcp add` writes
            fh.write('model = "x"\n\n[mcp_servers.halo]\ncommand = "halo-mcp"\n\n[other]\na = 1\n')
        integration.install(["codex"])
        integration.install(["codex"])  # idempotent
        text = open(cfg).read()
        self.assertEqual(text.count("default_tools_approval_mode"), 1)
        self.assertIn('[mcp_servers.halo]\ndefault_tools_approval_mode = "approve"', text)
        self.assertIn("[other]\na = 1", text)

    def test_mode_switch_applies_to_every_chosen_agent(self):
        integration.install(["claude", "codex"])
        integration.set_mode("frontier")
        self.assertNotIn(integration.BEGIN, self.rules("claude"))
        self.assertNotIn(integration.BEGIN, self.rules("codex"))
        integration.set_mode("hybrid")
        self.assertIn(integration.BEGIN, self.rules("codex"))
        st = integration.status()["agents"]
        self.assertTrue(st["codex"]["enabled"] and st["codex"]["rule_installed"])
        self.assertFalse(st["gemini"]["enabled"])

    def test_uninstall_keeps_user_text(self):
        os.makedirs(os.path.join(self.d, "codex"))
        with open(integration.AGENTS["codex"].rules_path(), "w") as fh:
            fh.write("# my codex rules\n")
        integration.install(["codex"])
        integration.uninstall("codex")
        self.assertEqual(self.rules("codex"), "# my codex rules\n")
        self.assertNotIn("codex", config.load().agents)
        self.assertTrue(any(c["args"][:2] == ["mcp", "remove"] for c in calls(self.log)))

    def test_old_installs_default_to_claude(self):
        self.assertEqual(integration.active_agents(config.Config()), ["claude"])

    def test_adding_an_agent_keeps_a_legacy_claude_install(self):
        integration.set_mode("hybrid")  # legacy: Claude rule, no agents list
        integration.install(["codex"])
        self.assertEqual(config.load().agents, ["claude", "codex"])

    def test_unknown_agent(self):
        with self.assertRaises(ValueError):
            integration.install(["copilot"])


if __name__ == "__main__":
    unittest.main()
