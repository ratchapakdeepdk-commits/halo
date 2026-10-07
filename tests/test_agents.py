"""Any agent can be in charge (Claude Code / Codex / Gemini) and any vendor CLI can be a paid
worker. Fake CLIs on PATH record their argv, stdin and environment."""
import io
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

    def test_council_asks_everyone_and_keeps_failures(self):
        doc = os.path.join(self.dir, "proof.md")
        with open(doc, "w") as fh:
            fh.write("E = mc^3 therefore ...")
        self.fake.replies = ["local says: exponent should be 2"]
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "gemini"}):
            r = tasks.council(self.cfg, "Is this derivation right?", [doc],
                              models=["claude", "gemini", "claude:opus", "qwen3:8b"])
        self.assertEqual((r["status"], r["answered"]), ("ok", "3/4"))
        by = {a["model"]: a for a in r["answers"]}
        self.assertEqual(list(by), ["claude", "gemini", "claude:opus", "qwen3:8b"])
        self.assertIn("return a + b", by["claude:opus"]["answer"])
        self.assertEqual(by["gemini"]["status"], "error")
        self.assertIn("quota exceeded", by["gemini"]["error"])
        self.assertIn("exponent", by["qwen3:8b"]["answer"])
        # every vendor saw the same material, with the question after it
        for c in calls(self.log):
            self.assertIn("E = mc^3", c["stdin"])
            self.assertLess(c["stdin"].index("E = mc^3"), c["stdin"].index("Is this derivation"))
            self.assertEqual(c["worker"], "1")
        rec = ledger.read(ledger.LEDGER)[-1]
        self.assertEqual(rec["kind"], "council")
        self.assertGreater(rec["cloud_calls"], 1)

    def test_council_refuses_huge_material(self):
        r = tasks.council(self.cfg, "q", text="x" * (tasks.COUNCIL_MAX_CHARS + 1), models=["codex"])
        self.assertEqual(r["status"], "error")
        self.assertEqual(calls(self.log) if os.path.exists(self.log) else [], [])

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

    def test_gui_add_remove_agent_and_council(self):
        from halo import gui
        import threading, urllib.error, urllib.request
        srv = gui.ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         method="POST", headers={"X-Halo-Token": gui.TOKEN})
            return json.load(urllib.request.urlopen(req))
        try:
            st = post("/api/agent", {"name": "codex", "action": "add"})
            codex = next(a for a in st["agents"] if a["name"] == "codex")
            self.assertTrue(codex["enabled"] and codex["cli"])
            self.assertIn(integration.BEGIN, self.rules("codex"))
            st = post("/api/agent", {"name": "codex", "action": "remove"})
            self.assertFalse(next(a for a in st["agents"] if a["name"] == "codex")["enabled"])
            self.assertEqual(self.rules("codex").count(integration.BEGIN), 0)

            self.assertEqual(st["council"]["members"], ["codex", "gemini"])  # default
            st = post("/api/council", {"members": ["claude", "gemini", "m-small", "evil"]})
            self.assertEqual(st["council"]["members"], ["claude", "gemini", "m-small"])
            self.assertEqual(config.load().council_models, ["claude", "gemini", "m-small"])
            with self.assertRaises(urllib.error.HTTPError) as e:
                post("/api/council", {"members": []})
            self.assertEqual(e.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as e:
                post("/api/agent", {"name": "rm -rf", "action": "add"})
            self.assertEqual(e.exception.code, 400)

            self.assertEqual((st["handoff"]["agent"], st["handoff"]["rounds"]), ("codex", 2))
            st = post("/api/handoff", {"agent": "claude:haiku", "rounds": 3})
            self.assertEqual((st["handoff"]["agent"], st["handoff"]["rounds"]), ("claude:haiku", 3))
            self.assertEqual(config.load().handoff_agent, "claude:haiku")
            for bad in ({"agent": "sh -c x", "rounds": 2}, {"agent": "codex", "rounds": 99}):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    post("/api/handoff", bad)
                self.assertEqual(e.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()

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


class TestCustomAgents(Env):
    """Any MCP-capable tool (or one that only runs shell commands) can be in charge."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.d, "home")
        self.log = os.path.join(self.d, "cli.log")
        self._env = mock.patch.dict(os.environ, {
            "HOME": self.home, "USERPROFILE": self.home, "FAKE_CLI_LOG": self.log,
            "PATH": make_bin(self.d, ("claude", "mytool")) + os.pathsep + os.environ["PATH"]})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def add(self, name, **spec):
        cfg = config.load()
        cfg.custom_agents = {**cfg.custom_agents,
                             name: integration.build_spec(name, **spec)}
        config.save(cfg)
        return integration.install([name])

    def path(self, rel):
        return os.path.join(self.home, rel)

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "w") as fh:
            fh.write(text)

    def read(self, rel):
        with open(self.path(rel)) as fh:
            return fh.read()

    def test_json_preset_keeps_the_users_config(self):
        self.write(".config/opencode/opencode.json",
                   json.dumps({"provider": {"halo": {"x": 1}}, "mcp": {"other": {"y": 2}}}))
        self.assertEqual(self.add("opencode"), 0)
        data = json.loads(self.read(".config/opencode/opencode.json"))
        self.assertEqual(data["provider"], {"halo": {"x": 1}})
        self.assertEqual(data["mcp"]["other"], {"y": 2})
        halo = data["mcp"]["halo"]
        self.assertEqual((halo["type"], halo["enabled"]), ("local", True))
        self.assertEqual(halo["command"], integration._server_cmd())  # {argv}: one list
        self.assertTrue(os.path.exists(self.path(".config/opencode/opencode.json.halo-bak")))
        self.assertIn(integration.BEGIN, self.read(".config/opencode/AGENTS.md"))
        self.assertIn("halo_digest", self.read(".config/opencode/AGENTS.md"))

        integration.set_mode("frontier")  # mode switches custom agents too (empty file: removed)
        self.assertFalse(os.path.exists(self.path(".config/opencode/AGENTS.md")))
        integration.set_mode("hybrid")
        integration.uninstall("opencode", forget=True)
        data = json.loads(self.read(".config/opencode/opencode.json"))
        self.assertNotIn("halo", data["mcp"])
        self.assertEqual(data["mcp"]["other"], {"y": 2})
        self.assertNotIn("opencode", config.load().custom_agents)

    def test_default_entry_and_nested_key(self):
        self.add("zedlike", mcp_file="~/.zed/settings.json", mcp_key="ctx.servers")
        data = json.loads(self.read(".zed/settings.json"))
        cmd = integration._server_cmd()
        self.assertEqual(data["ctx"]["servers"]["halo"], {"command": cmd[0], "args": cmd[1:]})
        st = integration.status()["agents"]["zedlike"]
        self.assertTrue(st["enabled"] and st["custom"] and st["cli"])
        self.assertEqual((st["kind"], st["rules"], st["rule_installed"]), ("json", "", False))

    def test_jsonc_is_refused_untouched(self):
        text = '{\n  // my comment\n  "mcpServers": {}\n}\n'
        self.write(".cursor/mcp.json", text)
        self.assertEqual(self.add("cursor"), 1)
        self.assertEqual(self.read(".cursor/mcp.json"), text)

    def test_agent_with_its_own_mcp_add_command(self):
        self.add("mine", cli="mytool", add=["mcp", "add", "halo", "{cmd}"],
                 remove=["mcp", "rm", "halo"], rules="~/.mytool/RULES.md")
        argv = [c["args"] for c in calls(self.log) if c["cli"] == "mytool"]
        self.assertEqual(argv[-1][:3], ["mcp", "add", "halo"])
        self.assertEqual(argv[-1][3:], integration._server_cmd())
        self.assertIn(integration.BEGIN, self.read(".mytool/RULES.md"))
        integration.uninstall("mine")
        self.assertEqual(calls(self.log)[-1]["args"], ["mcp", "rm", "halo"])

    def test_shell_only_agent_gets_commands_not_tools(self):
        self.add("aiderish", tools="cli", rules="~/.aider/HALO.md")
        rule = self.read(".aider/HALO.md")
        self.assertIn("halo digest -f", rule)
        self.assertIn("halo handoff -s", rule)
        self.assertNotIn("halo_digest", rule)

    def test_bad_specs(self):
        for name, spec in (("claude", {"mcp_file": "x"}),           # built in
                           ("Bad Name", {"mcp_file": "x"}),
                           ("t", {}),                                # no way to reach it
                           ("t", {"cli": "x", "add": ["mcp", "add"]}),  # no {cmd}
                           ("t", {"tools": "cli"}),                  # nowhere to put the rule
                           ("t", {"mcp_file": "x", "evil": 1})):
            with self.assertRaises(ValueError, msg=(name, spec)):
                integration.from_spec(name, spec)
        with self.assertRaises(ValueError):
            integration.build_spec("nopreset")
        cfg = config.load()
        cfg.custom_agents = {"broken": {"tools": "nope"}}
        self.assertNotIn("broken", integration.all_agents(cfg))  # skipped, not a crash

    def test_cli_add_list_remove(self):
        from halo import cli

        def run(argv):
            with self.assertRaises(SystemExit) as e, \
                    mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                cli.main(argv)
            return e.exception.code
        self.assertEqual(run(["agents", "add", "windsurf"]), 0)
        self.assertIn("halo", json.loads(self.read(".codeium/windsurf/mcp_config.json"))["mcpServers"])
        self.assertEqual(run(["agents", "add", "x1", "--cli-tools", "--rules",
                              os.path.join(self.home, "x1.md")]), 0)
        self.assertEqual(config.load().agents, ["windsurf", "x1"])
        self.assertEqual(run(["agents"]), 0)
        self.assertEqual(run(["agents", "add", "codex", "--mcp-file", "y"]), 2)
        self.assertEqual(run(["agents", "add", "nothing"]), 2)
        self.assertEqual(run(["agents", "remove", "windsurf"]), 0)
        self.assertNotIn("halo", json.loads(self.read(".codeium/windsurf/mcp_config.json"))["mcpServers"])
        self.assertEqual(sorted(config.load().custom_agents), ["x1"])

    def test_gui_preset_and_forget(self):
        from halo import gui
        import threading, urllib.request
        srv = gui.ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(body):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/agent",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"X-Halo-Token": gui.TOKEN})
            return json.load(urllib.request.urlopen(req))
        try:
            st = post({"name": "qwen", "action": "preset"})
            q = next(a for a in st["agents"] if a["name"] == "qwen")
            self.assertTrue(q["enabled"] and q["custom"])
            self.assertNotIn("qwen", [p["name"] for p in st["agent_presets"]])
            self.assertIn("halo", json.loads(self.read(".qwen/settings.json"))["mcpServers"])
            st = post({"name": "qwen", "action": "forget"})
            self.assertNotIn("qwen", [a["name"] for a in st["agents"]])
            self.assertIn("qwen", [p["name"] for p in st["agent_presets"]])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
