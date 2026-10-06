"""Vendor CLIs as templates: the built-in commands stay exactly what they were."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_cli import make_cli  # noqa: E402
from test_halo import Base  # noqa: E402
from halo import llm, vendors  # noqa: E402


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
