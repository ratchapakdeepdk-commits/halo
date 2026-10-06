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
