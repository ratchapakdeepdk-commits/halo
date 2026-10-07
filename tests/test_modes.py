import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_ollama import FakeOllama  # noqa: E402
from halo import config, gui, integration, ledger, mcp_server, tune  # noqa: E402


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.fake = FakeOllama(models=("m-small", "m-big", "llava:latest"))
        self.patches = [
            mock.patch.dict(os.environ, {"HALO_CLAUDE_DIR": os.path.join(self.d, "claude")}),
            mock.patch.object(config, "CONFIG_PATH", os.path.join(self.d, "cfg.json")),
            mock.patch.object(ledger, "LEDGER", os.path.join(self.d, "l.jsonl")),
        ]
        for p in self.patches:
            p.start()
        cfg = config.Config(ollama_url=self.fake.url, model="m-small")
        config.save(cfg)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.fake.close()
        self.tmp.cleanup()

    def claude_md(self):
        with open(os.path.join(self.d, "claude", "CLAUDE.md")) as fh:
            return fh.read()


class TestModes(Env):
    def test_rule_block_keeps_user_text(self):
        os.makedirs(os.path.join(self.d, "claude"))
        with open(os.path.join(self.d, "claude", "CLAUDE.md"), "w") as fh:
            fh.write("# My own rules\nbe nice\n")
        integration.set_mode("both")
        integration.set_mode("both")  # idempotent: block not duplicated
        text = self.claude_md()
        self.assertIn("be nice", text)
        self.assertEqual(text.count(integration.BEGIN), 1)
        self.assertIn("halo_digest", text)
        self.assertIn("halo_handoff", text)
        integration.set_mode("hybrid")
        text = self.claude_md()
        self.assertIn("halo_digest", text)
        self.assertNotIn("halo_handoff", text)
        integration.set_mode("hff")
        text = self.claude_md()
        self.assertEqual(text.count(integration.BEGIN), 1)
        self.assertIn("halo_handoff", text)
        self.assertNotIn("halo_digest", text)
        self.assertNotIn("free local model", text)
        integration.write_rule("off", "claude")
        self.assertEqual(self.claude_md().strip(), "# My own rules\nbe nice")

    def test_skill_only_with_local_model(self):
        integration.set_mode("both")
        self.assertTrue(integration.status()["skill_installed"])
        integration.set_mode("hff")
        self.assertFalse(integration.status()["skill_installed"])
        self.assertEqual(config.load().mode, "hff")
        integration.set_mode("hybrid")
        self.assertTrue(integration.status()["skill_installed"])

    def tools(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return [t["name"] for t in r["result"]["tools"]]

    def call(self, name, args):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
        return r["result"]

    def test_mcp_tools_follow_the_mode(self):
        integration.set_mode("hff")
        self.assertEqual(self.tools(), ["halo_council", "halo_handoff", "halo_stats"])
        # ...and a council there never reaches a local model, even when asked to
        r = self.call("halo_council", {"question": "x", "models": ["qwen3:8b"]})
        self.assertIn("no vendor models", r["content"][0]["text"])
        r = self.call("halo_ask", {"question": "x"})
        self.assertTrue(r["isError"])
        self.assertEqual(self.fake.requests, [])

        integration.set_mode("hybrid")
        self.assertEqual(self.tools(), ["halo_digest", "halo_code", "halo_ask",
                                        "halo_council", "halo_stats"])
        r = self.call("halo_handoff", {"task": "t", "sector": ["src"]})
        self.assertTrue(r["isError"])
        self.assertIn("hybrid mode", r["content"][0]["text"])
        # ...and its council never sends the user's files to a vendor
        r = self.call("halo_council", {"question": "x", "models": ["codex"]})
        self.assertIn("no local models", r["content"][0]["text"])

        integration.set_mode("both")
        self.assertEqual(len(self.tools()), 6)

    def test_old_config_names_migrate(self):
        def load_with(data):
            with open(config.CONFIG_PATH, "w") as fh:
                json.dump(data, fh)
            return config.load().mode
        # before mode_version, "hybrid" offered everything and "frontier" only the vendors
        self.assertEqual(load_with({"mode": "hybrid"}), "both")
        self.assertEqual(load_with({"mode": "frontier"}), "hff")
        self.assertEqual(load_with({"mode": "hybrid", "mode_version": 2}), "hybrid")
        self.assertEqual(load_with({"mode": "garbage", "mode_version": 2}), "both")

    def test_bad_mode_and_aliases(self):
        with self.assertRaises(ValueError):
            integration.set_mode("nope")
        self.assertEqual(integration.set_mode("frontier").mode, "hff")
        self.assertEqual(integration.set_mode("local").mode, "hybrid")

    def test_setup_hff_needs_no_ollama(self):
        from halo import cli
        with mock.patch.object(cli, "_detect_url", side_effect=AssertionError("Ollama probed")), \
                mock.patch.object(cli.llm, "pull", side_effect=AssertionError("model pulled")), \
                mock.patch.object(integration, "register_mcp", return_value=0), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            with self.assertRaises(SystemExit) as e:
                cli.main(["setup", "--mode", "hff", "-y", "--agent", "claude"])
        self.assertEqual(e.exception.code, 0, out.getvalue())
        self.assertEqual(config.load().mode, "hff")
        self.assertIn("halo_handoff", self.claude_md())
        self.assertIn("halo workers test codex", out.getvalue())


class TestTune(Env):
    def test_candidates_skip_vision_and_oversize(self):
        with mock.patch.object(tune.llm, "raw", return_value={"models": [
                {"name": "m-small", "size": 3 * 2**30}, {"name": "m-big", "size": 40 * 2**30},
                {"name": "llava:latest", "size": 4 * 2**30}]}):
            names = [n for n, _ in tune.candidates(config.load(), 16)]
        self.assertEqual(names, ["m-small"])

    def test_picks_and_saves(self):
        results = {"m-small": ({"ok": True, "seconds": 3}, {"passed": 1, "total": 3, "seconds": 20}),
                   "m-big": ({"ok": True, "seconds": 9}, {"passed": 3, "total": 3, "seconds": 50})}
        with mock.patch.object(tune, "test_digest", lambda c, m: results[m][0]), \
                mock.patch.object(tune, "test_code", lambda c, m: results[m][1]), \
                mock.patch.object(tune, "RESULTS", os.path.join(self.d, "tune.json")):
            out = tune.run(config.load(), models=["m-small", "m-big"], progress=lambda *_: None)
        self.assertEqual(out["chosen"], {"model": "m-small", "code_model": "m-big"})
        cfg = config.load()
        self.assertEqual((cfg.model, cfg.code_model, cfg.fallback_models),
                         ("m-small", "m-big", ["m-small"]))

    def test_synthetic_log_truth_matches_compaction(self):
        from halo import tasks
        text, truth = tune._synthetic_log()
        _, _, _, sig = tasks.compact_log(text)
        err = [g for g in sig if "connection refused" in g["line"]][0]
        self.assertEqual(err["count"], truth["errors"])


class TestCatalog(Env):
    def test_recommendations_fit_budget(self):
        from halo import catalog
        for budget in (4, 8, 12, 16, 24, 32, 48):
            picks = catalog.recommended(budget)
            self.assertTrue(picks, budget)
            self.assertTrue(all(m.need_gib <= budget for m in picks), budget)
        self.assertTrue(all(m.size_gib <= 9 for m in catalog.fitting(64, cpu_only=True)))

    def test_assign_roles(self):
        from halo import cli
        cfg = config.load()
        cli.assign_roles(cfg, ["qwen3.6:35b-a3b-q4_K_M", "qwen3:30b-a3b-instruct-2507-q4_K_M",
                               "gpt-oss:20b"])
        self.assertEqual((cfg.model, cfg.code_model, cfg.fallback_models),
                         ("qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3.6:35b-a3b-q4_K_M",
                          ["qwen3:30b-a3b-instruct-2507-q4_K_M"]))
        cli.assign_roles(cfg, ["qwen3:8b"])
        self.assertEqual((cfg.model, cfg.code_model, cfg.fallback_models), ("qwen3:8b", "", []))

    def test_pull_progress_and_error(self):
        from halo import llm
        msgs = []
        self.fake.pull_lines = [{"status": "pulling", "total": 100, "completed": 50},
                                {"status": "pulling", "total": 100, "completed": 100},
                                {"status": "success"}]
        self.assertTrue(llm.pull(config.load(), "m", progress=msgs.append))
        self.assertIn("m: ready", msgs)
        self.fake.pull_lines = [{"error": "pull model manifest: file does not exist"}]
        self.assertFalse(llm.pull(config.load(), "nope", progress=msgs.append))


class TestGui(Env):
    def test_token_required_and_mode_switch(self):
        srv = gui.ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            page = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn(gui.TOKEN, page)
            self.assertNotIn("__INIT__", page)
            req = urllib.request.Request(base + "/api/mode", data=b'{"mode":"hff"}',
                                         method="POST")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
            req.add_header("X-Halo-Token", gui.TOKEN)
            st = json.load(urllib.request.urlopen(req))
            self.assertEqual(st["mode"], "hff")
        finally:
            srv.shutdown()
            srv.server_close()



class TestGuiScript(unittest.TestCase):
    """One syntax error in the page script blanks the whole control panel (a missing `)`
    did, from 6 to 7 Oct 2026), and no other test runs the JavaScript."""

    def test_page_script_parses(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node or subprocess.run([node, "-e", "null ?? 1"], capture_output=True).returncode:
            self.skipTest("no node, or one too old for the syntax every browser has (??)")
        script = re.search(r"<script>(.*?)</script>", gui.PAGE, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(script)
        try:
            p = subprocess.run([node, "--check", fh.name], capture_output=True, text=True)
        finally:
            os.remove(fh.name)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])

if __name__ == "__main__":
    unittest.main()
