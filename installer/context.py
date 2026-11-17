"""The installer's world: the environment, every external command, every prompt and all
output go through one `Context`, so tests drive the whole flow with fakes and never touch the machine.

- `run(argv, limit, env, cwd) -> (exit code, output)`: the one runner. The real one starts each command in its own
  session, so a Ctrl-C at the terminal reaches the installer only, and the change in progress finishes.
- `ask(prompt) -> str`: the one input; raises EOFError when there is nothing to read.
- `write(text)`: the one output.
- HOME, KAIUSAP_HOME and PATH come from `environ`, never from the process.
"""
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent


class StepFailed(Exception):
    """A change that didn't finish: its reason and the command's output, if any."""

    def __init__(self, reason, output=""):
        super().__init__(reason)
        self.reason = reason
        self.output = output or ""


class Stop(Exception):
    """The installer stops before any change, with its message and exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.message = message
        self.code = code


def real_run(argv, limit=None, env=None, cwd=None):
    """The default runner: (exit code, stdout, with stderr appended when the command failed)."""
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=limit, env=env, cwd=cwd,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    out = completed.stdout if completed.returncode == 0 else completed.stdout + completed.stderr
    return completed.returncode, out


class Terminal:
    """Prompts read from /dev/tty, so `curl … | sh` can still ask; without a terminal the installer changes nothing."""

    def __init__(self):
        try:
            self.tty = open("/dev/tty")
        except OSError:
            self.tty = None

    @property
    def interactive(self):
        return self.tty is not None and sys.stdout.isatty()

    def ask(self, prompt):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        if self.tty is None:
            raise EOFError
        line = self.tty.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")


class Context:
    def __init__(self, environ=None, run=None, ask=None, write=None, interactive=False, repo=None, now=None,
                 python_version=None):
        self.environ = dict(os.environ if environ is None else environ)
        # install.sh put this checkout on PYTHONPATH for the installer alone; the commands it runs get the caller's.
        if "KAIUSAP_INSTALLER_PYTHONPATH" in self.environ:
            caller = self.environ.pop("KAIUSAP_INSTALLER_PYTHONPATH")
            if caller:
                self.environ["PYTHONPATH"] = caller
            else:
                self.environ.pop("PYTHONPATH", None)
        self._run = run or real_run
        self._ask = ask
        self.write = write or (lambda text: print(text, flush=True))
        self.interactive = interactive
        self.repo = Path(repo) if repo else REPO
        self.now = now or datetime.now
        self.python_version = tuple(python_version or sys.version_info[:3])
        self.home = Path(self.environ.get("HOME") or Path.home())
        self.kaiusap_home = Path(self.environ.get("KAIUSAP_HOME") or self.home / ".kaiusap")
        self.stamp = self.now().strftime("%y%m%d%H%M%S")  # one per run: every backup this run makes carries it
        self.stop_requested = False  # set by Ctrl-C while a change runs

    # ----- the one runner, the one input ---------------------------------------------------------------

    def run(self, argv, limit=None, env=None, cwd=None):
        return self._run([str(a) for a in argv], limit, self.environ if env is None else env,
                         None if cwd is None else str(cwd))

    def setup_runner(self, argv, limit, env=None):
        """`kaiusap.setup.Setup`'s runner shape, (argv, limit, env), over the same runner."""
        return self.run(argv, limit, env)

    def ask(self, prompt):
        if self._ask is None:
            raise EOFError
        return self._ask(prompt)

    def say(self, *lines):
        for line in lines:
            self.write(line)

    # ----- the environment ---------------------------------------------------------------------------

    def which(self, name):
        return shutil.which(name, path=self.environ.get("PATH", ""))

    def short(self, path):
        """A path with the home directory as `~`, for display."""
        home, text = str(self.home), str(path)
        return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text

    @property
    def bin_dir(self):
        """Where `uv tool install` puts `kaiusap`: UV_TOOL_BIN_DIR, else XDG_BIN_HOME, else ~/.local/bin."""
        given = self.environ.get("UV_TOOL_BIN_DIR") or self.environ.get("XDG_BIN_HOME")
        return Path(given) if given else self.home / ".local" / "bin"

    @property
    def kaiusap_bin(self):
        return self.bin_dir / "kaiusap"

    def iso_now(self):
        return self.now().astimezone().isoformat(timespec="seconds")
