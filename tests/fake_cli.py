"""Install a Python script as a runnable command, on POSIX and on Windows."""
import os
import stat
import sys


def make_cli(directory: str, name: str, source: str) -> str:
    """Write `source` (Python, no shebang) as command `name` in `directory`; return its path.

    POSIX gets an executable script with a shebang. Windows cannot run shebang scripts, so it
    gets name.py plus a name.cmd shim, which shutil.which() finds through PATHEXT.
    """
    os.makedirs(directory, exist_ok=True)
    if os.name == "nt":
        py = os.path.join(directory, name + ".py")
        with open(py, "w", encoding="utf-8") as fh:
            fh.write(source)
        cmd = os.path.join(directory, name + ".cmd")
        with open(cmd, "w", encoding="utf-8") as fh:
            fh.write(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n')
        return cmd
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!{sys.executable}\n{source}")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path
