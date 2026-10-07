"""Vendor CLIs as templates: the built-in commands stay exactly what they were."""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_cli import make_cli  # noqa: E402
from test_halo import Base  # noqa: E402
from halo import handoff, llm, vendors  # noqa: E402


def cmd(name, role, model=""):
    v = vendors.get(name)
    return vendors.command(v.worker if role == "worker" else v.agent, v.model_args,
                           bin="X", work="/w", model=model)


class TestTemplates(unittest.TestCase):
    def test_builtin_commands_unchanged(self):
        # the argv each CLI got before the templates, so the move changes nothing
        self.assertEqual(cmd("codex", "worker", "gpt-5"),
                         ["X", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                          "--ignore-user-config", "-s", "read-only", "-C", "/w",
                          "-m", "gpt-5", "-"])
        self.assertEqual(cmd("codex", "agent"),
                         ["X", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                          "--ignore-user-config", "-s", "workspace-write", "-C", "/w", "-"])
        self.assertEqual(cmd("claude", "worker", "haiku"),
                         ["X", "-p", "--output-format", "json", "--strict-mcp-config",
                          "--tools", "", "--no-session-persistence", "--model", "haiku"])
        self.assertEqual(cmd("claude", "agent"),
                         ["X", "-p", "--output-format", "json", "--strict-mcp-config",
                          "--tools", "Read,Edit,Write,Glob,Grep", "--permission-mode",
                          "acceptEdits", "--no-session-persistence"])
        self.assertEqual(cmd("gemini", "worker"),
                         ["X", "-o", "json", "--approval-mode", "plan",
                          "--allowed-mcp-server-names", "halo-worker-none", "-p", ""])
        self.assertEqual(cmd("gemini", "agent", "gemini-2.5-flash")[-4:],
                         ["-p", "", "-m", "gemini-2.5-flash"])

    def test_split(self):
        self.assertEqual(vendors.split("codex:gpt-5")[1], "gpt-5")
        self.assertEqual(vendors.split("claude")[0].name, "claude")
        self.assertEqual(vendors.split("qwen3:8b"), (None, ""))
        self.assertEqual(vendors.split(None), (None, ""))

    def test_placeholders_inside_an_element(self):
        self.assertEqual(vendors.command(("{bin}", "--msg={prompt_file}", "--cwd", "{work}"), (),
                                         bin="b", work="/w", model="", prompt_file="/p"),
                         ["b", "--msg=/p", "--cwd", "/w"])
        self.assertTrue(vendors.uses_prompt_file(("x", "--m={prompt_file}")))
        self.assertFalse(vendors.uses_prompt_file(vendors.get("codex").agent))


# A vendor that reads its prompt from a file and prints plain text: the shape a user-added
# CLI is likely to have, run through the same path as the built-ins.
FAKE = r'''import os, sys
args = sys.argv[1:]
prompt = open(args[args.index("--message-file") + 1]).read()
open(os.path.join(os.getcwd(), "seen.txt"), "w").write(prompt)
print("ANSWER " + prompt.splitlines()[-1])
'''


class TestTextVendor(Base):
    def setUp(self):
        super().setUp()
        exe = make_cli(os.path.join(self.dir, "bin"), "plain", FAKE)
        v = vendors.Vendor("plain", "Plain", worker=("{bin}", "--message-file", "{prompt_file}"),
                           agent=("{bin}", "--message-file", "{prompt_file}"),
                           model_args=("--model", "{model}"), output="text")
        p = mock.patch.dict(vendors.BUILTIN, {"plain": v})
        p.start()
        self.addCleanup(p.stop)
        self.cfg.plain_bin = exe

    def test_worker_prompt_file_and_text_output(self):
        usage = llm.Usage()
        self.assertEqual(llm.generate(self.cfg, "say hi", model="plain", usage=usage),
                         "ANSWER say hi")
        self.assertEqual(usage.cloud_calls, 0)  # plain text: tokens unknown, none counted

    def test_agent_prompt_file_stays_out_of_the_workspace(self):
        work = os.path.join(self.dir, "proj")
        os.makedirs(work)
        out = llm.cli_agent(self.cfg, "do it", "plain", work)
        self.assertEqual(out, "ANSWER do it")
        self.assertEqual(sorted(os.listdir(work)), ["seen.txt"])


if __name__ == "__main__":
    unittest.main()


# Fixes calc.py, leaves a helper process behind, then hangs: like a CLI that runs past its
# time after doing the work.
FAKE_HANG = r'''import os, subprocess, sys, time
sys.stdin.read()
with open("calc.py", "w") as fh:
    fh.write("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
subprocess.Popen([sys.executable, "-c",
                  "import time; time.sleep(6); open(%r, 'w').write('alive')" % os.environ["FAKE_MARK"]])
time.sleep(60)
'''


class TestSpecs(unittest.TestCase):
    def ok(self, **kw):
        return {"worker": ["{bin}"], **kw}

    def test_validation(self):
        bad = [("codex", self.ok()), ("Bad Name", self.ok()), ("x", {}),
               ("x", self.ok(oops=1)), ("x", self.ok(output="xml")),
               ("x", self.ok(output="jsonl")), ("x", {"worker": "run me"}),
               ("x", self.ok(worker_env={"A": 1}))]
        for name, spec in bad:
            with self.assertRaises(ValueError, msg=(name, spec)):
                vendors.from_spec(name, spec)
        v = vendors.from_spec("mine", self.ok(output="json", paths={"answer": "r"}))
        self.assertTrue(v.custom)
        self.assertEqual(v.agent, ())

    def test_presets_are_valid(self):
        for name, spec in vendors.PRESETS.items():
            vendors.from_spec(name, spec)

    def test_broken_spec_is_skipped_and_reported(self):
        cfg = type("C", (), {"custom_workers": {"good": {"worker": ["{bin}"]},
                                                "broke": {"output": "xml"}}})()
        self.assertEqual(list(vendors.custom(cfg)), ["good"])
        self.assertEqual(len(vendors.problems(cfg)), 1)
        self.assertTrue(llm.is_cloud("good:m", cfg))
        self.assertFalse(llm.is_cloud("good:m"))  # without the config: built-ins only
        self.assertFalse(llm.is_cloud("broke", cfg))

    def test_json_paths(self):
        v = vendors.from_spec("j", {"worker": ["{bin}"], "output": "json",
                                    "paths": {"answer": "out.0.text", "tokens_in": "u.in",
                                              "tokens_out": ["u.out", "u.think"]}})
        self.assertEqual(vendors.parse(v, 'log line\n{"out":[{"text":"hi"}],"u":{"in":5,"out":2,"think":1}}'),
                         ("hi", 5, 3, ""))

    def test_jsonl_last_answer_wins_and_tokens_sum(self):
        v = vendors.from_spec("opencode", vendors.PRESETS["opencode"])
        out = "\n".join(json.dumps(e) for e in [
            {"type": "text", "part": {"text": "looking"}},
            {"type": "step_finish", "part": {"tokens": {"input": 10, "output": 2,
                                                         "reasoning": 1, "cache": {"read": 5}}}},
            {"type": "text", "part": {"text": "done"}},
            {"type": "step_finish", "part": {"tokens": {"input": 20, "output": 3}}}]) + "\nnoise"
        self.assertEqual(vendors.parse(v, out), ("done", 35, 6, ""))
        err = vendors.parse(v, json.dumps({"type": "error", "error": {"name": "APIError"}}))
        self.assertIn("APIError", err[3])


# Behaves like opencode: prompt on stdin, JSONL events, permissions from an env variable,
# fixes calc.py when run as an agent.
FAKE_JSONL = r'''import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"args": args, "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
                         "prompt": prompt,
                         "perm": os.environ.get("OPENCODE_CONFIG_CONTENT")}) + "\n")
if os.environ.get("FAKE_MODE") == "error":
    print(json.dumps({"type": "error", "error": {"name": "APIError", "message": "quota"}}))
    sys.exit(1)
if "--auto" in args:
    with open("calc.py", "w") as fh:
        fh.write("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    text = "fixed calc.py"
else:
    text = "pong"
print(json.dumps({"type": "text", "part": {"text": text}}))
print(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 100, "output": 7}}}))
'''


class TestUserWorker(Base):
    def setUp(self):
        super().setUp()
        exe = make_cli(os.path.join(self.dir, "bin"), "oc", FAKE_JSONL)
        self.cfg.custom_workers = {"oc": dict(vendors.PRESETS["opencode"], bin=exe)}
        self.log = os.path.join(self.dir, "oc.log")
        env = mock.patch.dict(os.environ, {"FAKE_LOG": self.log, "FAKE_MODE": "ok"})
        env.start()
        self.addCleanup(env.stop)

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def test_worker_role(self):
        usage = llm.Usage()
        self.assertEqual(llm.generate(self.cfg, "hi", model="oc:halo/coder", usage=usage), "pong")
        self.assertEqual((usage.cloud_in, usage.cloud_out, usage.cloud_calls), (100, 7, 1))
        c = self.calls()[0]
        self.assertEqual(c["args"][-2:], ["-m", "halo/coder"])
        self.assertNotIn("--auto", c["args"])
        # JSON braces in the env value survive the placeholder filling
        self.assertEqual(json.loads(c["perm"])["permission"]["edit"], "deny")
        self.assertIn("Do NOT run shell commands", c["prompt"])

    def test_error_event_fails_the_run(self):
        os.environ["FAKE_MODE"] = "error"
        with self.assertRaises(llm.LocalModelError) as e:
            llm.generate(self.cfg, "hi", model="oc")
        self.assertIn("quota", str(e.exception))

    def test_probe_runs_worker_and_toy_handoff(self):
        r = handoff.probe(self.cfg, "oc:m")
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(set(r["steps"]), {"worker", "agent"})
        agent_call = self.calls()[1]
        self.assertIn("--auto", agent_call["args"])
        # cwd, $PWD and --dir all point at the scratch copy, never where HALO was started
        work = agent_call["args"][agent_call["args"].index("--dir") + 1]
        self.assertEqual(agent_call["pwd"], work)
        self.assertEqual(os.path.realpath(agent_call["cwd"]), os.path.realpath(work))
        self.assertNotEqual(os.path.realpath(work), os.path.realpath(os.getcwd()))
        self.assertEqual(json.loads(agent_call["perm"])["permission"]["bash"], "deny")
        self.assertIn("calc.py", agent_call["prompt"])

    def test_probe_fails_when_the_agent_does_nothing(self):
        self.cfg.custom_workers["oc"]["agent"] = ["{bin}", "run"]  # no --auto: no fix
        r = handoff.probe(self.cfg, "oc")
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["steps"]["worker"]["ok"])
        self.assertFalse(r["steps"]["agent"]["ok"])

    def test_worker_without_agent_command_cannot_take_a_handoff(self):
        del self.cfg.custom_workers["oc"]["agent"]
        r = handoff.handoff(self.cfg, "x", ["a.py"], workdir=self.dir, agent="oc")
        self.assertEqual(r["status"], "error")
        self.assertIn("no agent command", r.get("reason", "") + r.get("error", ""))
        self.assertEqual(list(handoff.probe(self.cfg, "oc")["steps"]), ["worker"])

    def test_run_check_is_off_by_default(self):
        r = handoff.probe(self.cfg, "oc")
        call = self.calls()[1]
        self.assertEqual(json.loads(call["perm"])["permission"]["bash"], "deny")
        self.assertNotIn("You may run exactly", call["prompt"])
        self.assertEqual(r["status"], "passed")

    def test_run_check_allows_exactly_the_check(self):
        self.cfg.custom_workers["oc"]["run_check"] = True
        proj = os.path.join(self.dir, "p")
        os.makedirs(proj)
        with open(os.path.join(proj, "calc.py"), "w") as fh:
            fh.write("x = 1\n")
        check = f'"{sys.executable}" -c "import calc"'
        r = handoff.handoff(self.cfg, "fix", ["calc.py"], workdir=proj, agent="oc", check=check)
        self.assertTrue(r["agent_ran_check"])
        call = self.calls()[-1]
        bash = json.loads(call["perm"])["permission"]["bash"]
        self.assertEqual(bash, {"*": "deny", check: "allow"})  # quotes survive the JSON
        self.assertIn("You may run exactly that check command", call["prompt"])

    def test_run_check_refuses_globs(self):
        self.cfg.custom_workers["oc"]["run_check"] = True
        proj = os.path.join(self.dir, "p")
        os.makedirs(proj)
        r = handoff.handoff(self.cfg, "fix", ["calc.py"], workdir=proj, agent="oc",
                            check="python3 -m pytest tests/*")
        self.assertFalse(r["agent_ran_check"])
        self.assertEqual(json.loads(self.calls()[-1]["perm"])["permission"]["bash"], "deny")

    def test_run_check_needs_check_env(self):
        with self.assertRaises(ValueError):
            vendors.from_spec("x", {"agent": ["{bin}"], "run_check": True})
        with self.assertRaises(ValueError):
            vendors.from_spec("x", {"agent": ["{bin}"], "run_check": "yes",
                                    "check_env": {"A": "{check}"}})

    def test_gui_choices(self):
        from halo import gui
        self.cfg.handoff_agent = "oc:halo/coder"
        ch = gui._handoff_choices(self.cfg)
        self.assertEqual(ch[:5], ["codex", "claude", "claude:haiku", "claude:sonnet", "gemini"])
        self.assertIn("oc", ch)
        self.assertIn("oc:halo/coder", ch)
        self.assertTrue(gui._handoff_ok(self.cfg, "oc:anything"))
        self.assertFalse(gui._handoff_ok(self.cfg, "qwen3:8b"))
        self.assertIn("oc", gui._council_choices(self.cfg, []))


class TestWorkersCommand(Base):
    def setUp(self):
        super().setUp()
        from halo import config
        config.save(self.cfg)

    def run_cli(self, *argv):
        from halo import cli, config
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit) as e:
                cli.main(list(argv))
        return e.exception.code, out.getvalue() + err.getvalue(), config.load()

    def test_add_list_remove(self):
        exe = make_cli(os.path.join(self.dir, "bin"), "oc", FAKE_JSONL)
        code, out, _ = self.run_cli("workers", "add", "oc")
        self.assertEqual(code, 2)  # no preset named oc and no --spec
        spec = json.dumps({"worker": ["{bin}"], "agent": ["{bin}", "--auto"]})
        code, out, cfg = self.run_cli("workers", "add", "oc", "--spec", spec, "--bin", exe, "-y")
        self.assertEqual(code, 0, out)
        self.assertIn("cannot sandbox", out)
        self.assertEqual(cfg.custom_workers["oc"]["bin"], os.path.abspath(exe))
        code, out, _ = self.run_cli("workers")
        self.assertIn("oc", out)
        self.assertIn("yours", out)
        cfg.fallback_models = ["qwen3:8b", "oc:m"]
        cfg.handoff_agent = "oc"
        from halo import config
        config.save(cfg)
        code, out, cfg = self.run_cli("workers", "remove", "oc")
        self.assertEqual(code, 0, out)
        self.assertEqual((cfg.custom_workers, cfg.fallback_models, cfg.handoff_agent),
                         ({}, ["qwen3:8b"], "codex"))

    def test_add_refuses_builtin_and_needs_yes_without_tty(self):
        self.assertEqual(self.run_cli("workers", "add", "codex")[0], 2)
        with mock.patch("sys.stdin", io.StringIO("")):
            code, out, cfg = self.run_cli("workers", "add", "opencode")
        self.assertEqual(code, 2)
        self.assertEqual(cfg.custom_workers, {})


class FakeAPI:
    """A minimal OpenAI-compatible /v1/chat/completions that records requests."""
    def __init__(self, reply="pong", status=200):
        import http.server
        import threading
        outer = self
        self.requests, self.reply, self.status = [], reply, status

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": body})
                data = json.dumps({"choices": [{"message": {"content": outer.reply}}],
                                   "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
                                  if outer.status == 200 else {"error": "bad key"}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TestAPIWorker(Base):
    def setUp(self):
        super().setUp()
        self.api = FakeAPI()
        self.addCleanup(self.api.close)
        env = mock.patch.dict(os.environ, {"FAKE_KEY": "sk-test"})
        env.start()
        self.addCleanup(env.stop)
        self.cfg.custom_workers = {"myapi": {"api": self.api.url, "key_env": "FAKE_KEY",
                                             "model": "m1"}}

    def test_answer_tokens_and_key(self):
        usage = llm.Usage()
        self.assertEqual(llm.generate(self.cfg, "hi", system="be brief", model="myapi",
                                      usage=usage), "pong")
        r = self.api.requests[0]
        self.assertEqual((r["path"], r["auth"], r["body"]["model"]),
                         ("/v1/chat/completions", "Bearer sk-test", "m1"))
        self.assertEqual(r["body"]["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual((usage.cloud_in, usage.cloud_out, usage.cloud_calls), (12, 3, 1))
        llm.generate(self.cfg, "hi", model="myapi:m2")
        self.assertEqual(self.api.requests[1]["body"]["model"], "m2")

    def test_key_never_in_config(self):
        spec = vendors.build_spec("x", api="https://h.example/v1", key_env="FAKE_KEY")
        self.assertNotIn("sk-test", json.dumps(spec))

    def test_missing_key_and_http_errors(self):
        del os.environ["FAKE_KEY"]
        with self.assertRaises(llm.LocalModelError) as e:
            llm.generate(self.cfg, "hi", model="myapi")
        self.assertIn("FAKE_KEY", str(e.exception))
        self.assertEqual(self.api.requests, [])
        os.environ["FAKE_KEY"] = "k"
        self.api.status = 401
        with self.assertRaises(llm.LocalModelError) as e:
            llm.generate(self.cfg, "hi", model="myapi")
        self.assertIn("401", str(e.exception))

    def test_plain_http_only_to_this_machine(self):
        with self.assertRaises(ValueError):
            vendors.build_spec("x", api="http://api.example.com/v1")
        vendors.build_spec("x", api="http://127.0.0.1:8820/v1")
        vendors.build_spec("x", api="http://localhost:8820/v1")

    def test_api_is_text_only(self):
        with self.assertRaises(llm.LocalModelError) as e:
            llm.cli_agent(self.cfg, "do", "myapi", self.dir)
        self.assertIn("text only", str(e.exception))
        r = handoff.probe(self.cfg, "myapi")
        self.assertEqual((r["status"], list(r["steps"])), ("passed", ["worker"]))

    def test_api_worker_as_code_fallback(self):
        from test_halo import BAD, CHECK, GOOD
        from halo import tasks
        self.api.reply = GOOD
        self.cfg.fallback_models = ["myapi"]
        self.fake.replies = [BAD] * 5
        r = tasks.code(self.cfg, "add two numbers", "mod.py", CHECK, workdir=self.dir,
                       max_iters=1)
        self.assertEqual((r["status"], r["model"]), ("passed", "myapi"))


class TestGuiWorkers(Base):
    def setUp(self):
        super().setUp()
        from halo import config
        config.save(self.cfg)

    def post(self, body):
        from halo import gui
        return gui._workers_post(body)

    def test_add_needs_the_box_ticked(self):
        r, code = self.post({"action": "add", "name": "ds", "preset": "deepseek"})
        self.assertEqual(code, 400)
        self.assertIn("sandbox", r["error"])

    def test_add_preset_api_and_cli_then_remove(self):
        from halo import config
        r, code = self.post({"action": "add", "name": "ds", "preset": "deepseek",
                             "understood": True})
        self.assertEqual(code, 200, r)
        r, code = self.post({"action": "add", "name": "local", "understood": True,
                             "api": "http://127.0.0.1:1/v1", "key_file": "~/k"})
        self.assertEqual(code, 200, r)
        r, code = self.post({"action": "add", "name": "mine", "understood": True,
                             "spec": '{"worker": ["{bin}"]}', "bin": "/usr/bin/true"})
        self.assertEqual(code, 200, r)
        cfg = config.load()
        self.assertEqual(set(cfg.custom_workers), {"ds", "local", "mine"})
        self.assertEqual(cfg.custom_workers["mine"]["bin"], os.path.abspath("/usr/bin/true"))
        names = {v["name"] for v in r["vendors"]}
        self.assertTrue({"ds", "local", "mine"} <= names)
        r, code = self.post({"action": "remove", "name": "ds"})
        self.assertEqual(code, 200)
        self.assertNotIn("ds", config.load().custom_workers)

    def test_bad_input_is_a_400_not_a_crash(self):
        for body in [{"action": "add", "name": "codex", "understood": True, "preset": "deepseek"},
                     {"action": "add", "name": "x", "understood": True, "spec": "{not json"},
                     {"action": "add", "name": "x", "understood": True,
                      "api": "http://evil.example/v1"},
                     {"action": "remove", "name": "codex"},
                     {"action": "test", "name": "qwen3:8b"},
                     {"action": "nope"}]:
            r, code = self.post(body)
            self.assertEqual(code, 400, body)
            self.assertIn("error", r)


class TestTimeout(Base):
    def test_timeout_still_runs_the_check_and_kills_the_group(self):
        import time
        exe = make_cli(os.path.join(self.dir, "bin"), "hang", FAKE_HANG)
        self.cfg.custom_workers = {"hang": {"agent": ["{bin}"], "bin": exe}}
        self.cfg.handoff_timeout = 3
        mark = os.path.join(self.dir, "helper-alive")
        t0 = time.time()
        with mock.patch.dict(os.environ, {"FAKE_MARK": mark}):
            r = handoff.probe(self.cfg, "hang")
        self.assertEqual(r["steps"]["agent"]["status"], "passed", r)
        # The helper writes 6 s after it starts and the kill comes at 3 s: a slow taskkill
        # on a Windows runner still lands first. Wait until a surviving helper would have written.
        time.sleep(max(0.0, 8.5 - (time.time() - t0)))
        self.assertFalse(os.path.exists(mark), "the CLI's helper outlived the timeout")
