"""Every bench task's reference solution must pass its own tests (proves the tests are fair)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")


class BenchReferenceTest(unittest.TestCase):
    def test_references_pass(self):
        tasks_dir = os.path.join(BENCH, "tasks")
        for name in sorted(os.listdir(tasks_dir)):
            with self.subTest(task=name):
                with open(os.path.join(tasks_dir, name, "task.json")) as fh:
                    target = json.load(fh)["target"]
                work = tempfile.mkdtemp()
                try:
                    shutil.copy(os.path.join(tasks_dir, name, "test_task.py"), work)
                    shutil.copy(os.path.join(BENCH, "reference", target), work)
                    r = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_task"],
                                       cwd=work, capture_output=True, text=True, timeout=60)
                    self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                finally:
                    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
