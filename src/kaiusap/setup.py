"""`kaiusap setup`: what is in place on this machine, read-only.

Each item says what it rests on. Nothing here writes: no store record, no
terminal-map line, no provider call beyond two version probes with time limits,
and no tmux call but the pane-titles item's one read.
The shared items are here; each registered adapter gives its own (`setup_items`).
"""
import json
import os
from pathlib import Path
import shutil
import tomllib

from .adapters import ADAPTERS, adapter_class, display_name
from .adapters.daemon import run_command
from .model import load_settings

PLUGIN_ID = "kaiusap@kaiusap"  # every provider's plugin and marketplace
MARKETPLACE = "kaiusap"
TMUX_WARNING = "kaiusap looks at the default tmux server only"
TMUX_READ_LIMIT = 2  # seconds for the pane-titles item's one tmux read, as for kaiusap's other tmux calls
SET_TITLE_FIX = "set -g allow-set-title on\ntmux source-file ~/.tmux.conf"
INSTALL_TOOL = "uv tool install {repo}"
LAUNCHER_MARK = b"kaiusap-plugin-launcher"  # in each plugin's bin/kaiusap, which skips any file holding it


def is_launcher(path):
    """Whether `path` is a plugin's launcher, not the installed program."""
    try:
        with open(path, "rb") as stream:
            return LAUNCHER_MARK in stream.read(4096)
    except OSError:
        return False


def repo_source():
    """The install source recorded in the package metadata (PEP 610 direct_url.json), or None."""
    try:
        from importlib.metadata import distributions
        for dist in distributions():
            if dist.metadata["Name"] == "kaiusap" and (text := dist.read_text("direct_url.json")):
                return direct_url_source(text)
    except Exception:  # no metadata: no source
        pass
    return None


def direct_url_source(text):
    """`<repo>` from a direct_url.json text: a local path for file URLs, else the URL without `git+`."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return unquote(urlparse(url).path) or None
    return url.removeprefix("git+")


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, UnicodeError):
        return None


def read_toml(path):
    try:
        with open(path, "rb") as stream:
            return tomllib.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None


def short(path):
    """A path with the home directory as `~`, for display."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def answers(socket_path):
    """Whether a server is listening on a Unix socket: a left-behind socket refuses the connection."""
    import socket
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class Setup:
    """The context an adapter's `setup_items` reads: the environment, the command runner, the home
    directory and the install source, and the item shape."""

    def __init__(self, store, environ=None, run=None, home=None, repo=None, options=None):
        self.store = store
        self.options = dict(options or {})  # the adapters' own setup flags, by key
        self.memo = {}  # what adapters read once per report, by their own keys
        self.environ = os.environ if environ is None else environ
        self.run = run or run_command
        self.home = Path(home) if home else Path.home()
        self.repo = repo if repo is not None else (repo_source() or "<repo>")

    def command(self, *templates):
        """Install commands, one per line, with `{repo}` the install source."""
        return "\n".join(template.format(repo=self.repo) for template in templates)

    @staticmethod
    def item(name, done, detail=None, command=None, note=None):
        return {"item": name, "done": done, "detail": detail, "command": None if done else command, "note": note}

    def report(self):
        load_settings(self.store)  # a malformed settings.json is reported
        # The order: the program the plugins find, then the providers' items at the positions their adapters give, then tmux.
        items = [self.path()]
        provided = [entry for name in ADAPTERS for entry in self.provider_items(name)]
        items.extend(item for _, item in sorted(provided, key=lambda entry: entry[0]))
        return {"items": [*items, self.tmux(), self.pane_titles()]}

    def provider_items(self, name):
        """A provider's items, or one line in their place when its command isn't on this shell's PATH and none of
        its items is done: a provider that isn't installed is skipped, as the installer skips it."""
        adapter = adapter_class(name)
        items = adapter.setup_items(self)
        command = adapter.setup_command(self)
        if not items or not command or any(item["done"] for _, item in items):
            return items
        if os.sep in command:
            present = os.path.isfile(command) and os.access(command, os.X_OK)
        else:
            present = shutil.which(command, path=self.environ.get("PATH", "")) is not None
        if present:
            return items
        place = min(entry[0] for entry in items)
        return [(place, self.item(display_name(name), True, f"{command} isn't on the PATH; its items are skipped"))]

    # ----- the shared items ---------------------------------------------------

    def path(self):
        """The program the plugins' launcher would run, by its rule:
        KAIUSAP_BIN, then ~/.local/bin/kaiusap, then `kaiusap` on this shell's PATH, skipping a launcher."""
        found = self.launched_program()
        return self.item("kaiusap found by the plugins", bool(found), found or "not found", self.command(INSTALL_TOOL))

    def launched_program(self):
        def executable(path):
            return os.path.isfile(path) and os.access(path, os.X_OK)
        explicit = self.environ.get("KAIUSAP_BIN") or ""
        if explicit and executable(explicit):
            return explicit
        local = str(self.home / ".local" / "bin" / "kaiusap")
        if executable(local):
            return local
        for folder in self.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(folder or ".", "kaiusap")
            if executable(candidate) and not is_launcher(candidate):
                return candidate
        return None

    def tmux(self):
        """A warning when this shell's TMUX names a socket other than the default server's."""
        tmux = self.environ.get("TMUX") or ""
        if not tmux:
            return self.item("tmux server", True, "not in tmux")
        socket = Path(tmux.rsplit(",", 2)[0])
        if socket.name == "default" and socket.parent.name == f"tmux-{os.getuid()}":
            return self.item("tmux server", True, "the default server")
        return self.item("tmux server", False, f"this shell's TMUX names {socket}; {TMUX_WARNING}")

    def pane_titles(self):
        """tmux's `allow-set-title` on the default server, when one is running: its only tmux call, one read.
        kaiusap reads pane titles and names windows from them; with the option off a program's
        title never reaches the pane."""
        import subprocess
        name = "tmux pane titles"
        # The default server's socket: tmux-<uid>/default under TMUX_TMPDIR, else /tmp, as tmux finds it.
        socket = Path(self.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / "default"
        if not socket.exists() or not answers(socket):
            # A socket no server answers on is left from a server that ended.
            return self.item(name, True, "no default tmux server is running")
        command = ["tmux", "-L", "default", "show-options", "-gwv", "allow-set-title"]
        fix = "add the first line to ~/.tmux.conf, then run the second"
        try:
            code, out = self.run(command, TMUX_READ_LIMIT, self.environ)
        except subprocess.TimeoutExpired:
            return self.item(name, False, f"{' '.join(command)} didn't finish in {TMUX_READ_LIMIT} s; {fix}", SET_TITLE_FIX)
        except (OSError, ValueError) as exc:
            return self.item(name, False, f"{' '.join(command)} couldn't be run ({type(exc).__name__}); {fix}", SET_TITLE_FIX)
        value = out.strip() if code == 0 else None
        if value == "on":
            return self.item(name, True, "allow-set-title is on")
        detail = ("allow-set-title is off, so a program's title never reaches its pane" if value == "off" else
                  f"{' '.join(command)} exited {code}" if code else f"{' '.join(command)} printed {value!r}")
        return self.item(name, False, f"{detail}; {fix}", SET_TITLE_FIX)
