"""Which OpenCode provider `kaiusap setup` shows, shared by
OpenCode 1.x's adapter and OpenCode 2's. Detection suggests, the user chooses: the version found suggests a
provider, an installed kaiusap plugin keeps the user's choice, and `--opencode 1|2` shows that provider whatever
is found. Read-only, as all of `setup`: one `<command> --version` (`--opencode-command`'s, else `opencode`),
and reads of the config directory (`--opencode-config`'s, else the default one).
"""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from ..errors import Error

VERSION_LIMIT = 3
V2 = (2, 0, 0)
NAMES = {1: "opencode", 2: "opencode2"}
# Each provider's plugin, as (the folder under the config directory, the link's name, the package file it links to).
PLUGINS = {1: ("plugin", "kaiusap.ts", "plugins/opencode/kaiusap.ts"), 2: ("plugins", "kaiusap", "plugins/opencode2")}
SAFE = re.compile(r"[\w@%+=:,./~-]+")


def parse_version(text):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def config_dir(setup):
    """The OpenCode config directory checked: `--opencode-config`, else the one the `opencode` on the PATH uses."""
    given = setup.options.get("opencode_config")
    if given:
        return Path(str(given)).expanduser()
    base = setup.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else setup.home / ".config") / "opencode"


def shown(setup, path):
    """A path for a detail or a command: under the home directory as `~/...`, else whole; quoted for the shell
    when it needs it (then whole, since a quoted `~` isn't expanded)."""
    home, text = str(setup.home), str(path)
    display = "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text
    return display if SAFE.fullmatch(display) else shlex.quote(text)


def plugin_command(setup, provider):
    """The command that installs `provider`'s plugin (1 or 2) into the config directory checked."""
    folder, name, source = PLUGINS[provider]
    directory = shown(setup, config_dir(setup) / folder).replace("{", "{{").replace("}", "}}")
    return setup.command(f"mkdir -p {directory} && ln -s {{repo}}/{source} {directory}/{name}")


def command(setup):
    """The OpenCode command whose version is read: `--opencode-command`, else `opencode`."""
    given = setup.options.get("opencode_command")
    return os.path.expanduser(str(given)) if given else "opencode"


def probe(setup):
    """`<command> --version` (3 s), only when the command is on the PATH (or, given as a path, is there), once per
    report: `{version, detail, command}`, with `version` None and the reason in `detail` when none was read."""
    if "opencode_version" in setup.memo:
        return setup.memo["opencode_version"]
    name = command(setup)
    found = {"version": None, "detail": f"{name} isn't on the PATH", "command": None}
    if shutil.which(name, path=setup.environ.get("PATH", "")):
        try:
            code, out = setup.run([name, "--version"], VERSION_LIMIT, setup.environ)
            version = parse_version(out) if not code else None
            found = ({"version": version, "detail": ".".join(map(str, version)), "command": None} if version else
                     {"version": None, "detail": f"{name} --version printed no version", "command": "opencode upgrade"})
        except subprocess.TimeoutExpired:
            found = {"version": None, "detail": f"{name} --version didn't finish in {VERSION_LIMIT} s",
                     "command": "opencode upgrade"}
        except (OSError, ValueError):
            pass
    setup.memo["opencode_version"] = found
    return found


def installed(directory):
    """The kaiusap OpenCode plugins in a config directory: 1 for 1.x's module, 2 for OpenCode 2's directory."""
    found = set()
    if any((directory / folder / "kaiusap.ts").is_file() for folder in ("plugin", "plugins")):
        found.add(1)
    if (directory / "plugins" / "kaiusap" / "server.ts").is_file():
        found.add(2)
    return found


def choice(setup):
    """`{shown, suggested, version, installed, forced}`: the provider whose items `setup` shows (1 or 2)."""
    if "opencode_choice" in setup.memo:
        return setup.memo["opencode_choice"]
    forced = setup.options.get("opencode")
    if forced is not None and str(forced) not in {"1", "2"}:
        raise Error("--opencode must be 1 or 2.", 64)
    version = probe(setup)["version"]
    suggested = (2 if version >= V2 else 1) if version else None
    plugins = installed(config_dir(setup))
    if forced is not None:
        shown = int(forced)
    elif len(plugins) == 1:
        shown = next(iter(plugins))  # the user's choice stands
    else:
        shown = suggested or 1
    result = {"shown": shown, "suggested": suggested, "version": version, "installed": plugins,
              "forced": forced is not None}
    setup.memo["opencode_choice"] = result
    return result


def suggestion_note(found):
    """The note on a plugin item when neither plugin is installed and the user hasn't chosen."""
    if found["forced"] or found["installed"] or not found["suggested"]:
        return None
    other = 3 - found["suggested"]
    return (f"kaiusap suggests its OpenCode {found['suggested']} provider for OpenCode "
            f"{'.'.join(map(str, found['version']))}; for the other, run kaiusap setup --opencode {other}")


def strip_jsonc(text):
    """JSON with comments and trailing commas, as OpenCode reads `opencode.jsonc`, made plain JSON (both versions' MCP items)."""
    out, i, quote = [], 0, False
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                quote = False
        elif c == '"':
            quote = True
            out.append(c)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def runs_kaiusap_mcp(command, provider):
    """An OpenCode MCP entry's `command` runs kaiusap's MCP server for `provider`: `kaiusap` by name, as `setup`
    prints it, or by its full path, as the installer writes it."""
    return (isinstance(command, list) and len(command) == 3 and isinstance(command[0], str)
            and os.path.basename(command[0]) == "kaiusap" and command[1:] == ["mcp", provider])
