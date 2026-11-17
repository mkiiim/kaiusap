#!/bin/sh
# kaiusap's installer. Run from a checkout:
#   ./install.sh                 install, or upgrade, or finish a stopped run
#   ./install.sh --plan          print the check and the plan, change nothing
#   ./install.sh --uninstall     remove what the installer added
#   ./install.sh --rollback      go back to the version the last upgrade replaced
#   --opencode 1|2  --opencode-command CMD  --opencode-config DIR   as `kaiusap setup` takes them
#
# This script checks only what the installer itself needs to start: Python 3.12 or later, uv and rustc,
# every problem in one list. The installer checks everything else, still before changing anything.
set -u

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
if [ ! -f "$here/installer/flow.py" ] || [ ! -f "$here/pyproject.toml" ]; then
    echo "Run ./install.sh from a kaiusap checkout. Nothing was changed." >&2
    exit 1
fi

problems=0
report=""
add() {
    report="$report$1
"
}
# One check line: mark, label padded to the installer's column, detail.
check() {
    add "$(printf '  %s %-22s %s' "$1" "$2" "$3")"
}

python=""
for candidate in python3 python python3.14 python3.13 python3.12; do
    found=$(command -v "$candidate" 2>/dev/null) || continue
    if "$found" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        python=$found
        break
    fi
done
# A Python uv manages, which needn't be on the PATH (`uv python install 3.12`); `find` never downloads one.
if [ -z "$python" ] && command -v uv >/dev/null 2>&1; then
    python=$(uv python find '>=3.12' 2>/dev/null) || python=""
fi
if [ -n "$python" ]; then
    version=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    check ✓ "Python $version" "needs 3.12 or later"
else
    seen=""
    if found=$(command -v python3 2>/dev/null); then
        seen=$("$found" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || seen=""
    fi
    if [ -n "$seen" ]; then
        check ✗ "Python $seen" "needs 3.12 or later"
    else
        check ✗ "Python" "not found"
    fi
    add "       fix: install Python 3.12 or later (https://www.python.org/downloads/, or: uv python install 3.12)"
    problems=$((problems + 1))
fi

if command -v uv >/dev/null 2>&1; then
    check ✓ uv "installs kaiusap"
else
    check ✗ uv "not found"
    add "       fix: install uv: curl -LsSf https://astral.sh/uv/install.sh | sh, then open a new terminal"
    problems=$((problems + 1))
fi

if command -v rustc >/dev/null 2>&1 && rustc --version >/dev/null 2>&1; then
    check ✓ rustc "builds kaiusap's hook program"
elif command -v rustc >/dev/null 2>&1; then
    # Found but unusable, e.g. rustup with no default toolchain: the build would fail at the first change.
    check ✗ rustc "found, but rustc --version fails"
    add "       kaiusap's hook program is built with Rust."
    add "       fix: rustup default stable (or install Rust from https://rustup.rs), then open a new terminal"
    if [ -n "$python" ] && command -v uv >/dev/null 2>&1; then :; else problems=$((problems + 1)); fi
else
    check ✗ rustc "not found"
    add "       kaiusap's hook program is built with Rust."
    add "       fix: install Rust from https://rustup.rs, then open a new terminal"
    # With Python and uv present the installer runs anyway, so a missing rustc is listed with every other problem.
    if [ -n "$python" ] && command -v uv >/dev/null 2>&1; then :; else problems=$((problems + 1)); fi
fi

if [ "$problems" -gt 0 ]; then
    echo "Checking this machine. Nothing is changed yet."
    printf '%s' "$report"
    echo
    if [ "$problems" -eq 1 ]; then echo "1 problem. Nothing was changed."; else echo "$problems problems. Nothing was changed."; fi
    echo "Fix them, then run ./install.sh again."
    exit 1
fi

# The installer imports itself and kaiusap's checks from this checkout; the commands it runs must not (an installed
# kaiusap would import the checkout's code), so it puts the caller's PYTHONPATH back for them.
KAIUSAP_INSTALLER_PYTHONPATH="${PYTHONPATH-}"
PYTHONPATH="$here:$here/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH KAIUSAP_INSTALLER_PYTHONPATH
cd "$here" || exit 1
# tomlkit (format-preserving TOML) is the installer's own dependency, not kaiusap's.
exec uv run --no-project --python "$python" --with tomlkit python -m installer "$@"
