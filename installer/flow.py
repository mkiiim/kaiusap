"""The installer's flow:
check, recommend closing every session, plan, ask once, change in order, verify, close with one list.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import checks, plan as plans, record as records, sessions
from .context import Stop, StepFailed

NUMBERS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"]
NAME_EXAMPLES = {"claude": "api-cld", "codex": "web-cdx", "opencode": "web-oc"}
VERIFY_LIMIT = 60
STORE_ASK = "Ask about your mail in {home}/"


def parse(argv):
    parser = argparse.ArgumentParser(prog="./install.sh", description="Install, upgrade, roll back or uninstall kaiusap.")
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--uninstall", action="store_true", help="remove what the installer added")
    what.add_argument("--rollback", action="store_true", help="go back to the version the last upgrade replaced")
    parser.add_argument("--opencode", choices=["1", "2"], help="kaiusap's OpenCode 1 or 2 plugin, whatever is found")
    parser.add_argument("--opencode-command", metavar="CMD", help="the OpenCode command, if not opencode")
    parser.add_argument("--opencode-config", metavar="DIR", help="the OpenCode config directory, if not the default")
    parser.add_argument("--plan", action="store_true", help="print the check and the plan, and change nothing")
    return parser.parse_args(argv)


def main(argv, ctx):
    try:
        opts = parse(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 64
    if opts.opencode_config and opts.opencode_config.startswith("~"):
        opts.opencode_config = str(ctx.home) + opts.opencode_config[1:]
    try:
        if opts.uninstall:
            return uninstall(ctx, opts)
        if opts.rollback:
            return rollback(ctx, opts)
        return install(ctx, opts)
    except Stop as stop:
        ctx.say("", stop.message)
        return stop.code
    except KeyboardInterrupt:
        ctx.say("", "Stopped. Nothing was changed.")
        return 130


# ----- shared pieces ------------------------------------------------------------------------------------------

def when(ctx, iso):
    """`15:58 today`, or `15:58 on 2026-11-12`."""
    try:
        at = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return "an unknown time"
    today = ctx.now().astimezone().date()
    return at.strftime("%H:%M") + (" today" if at.date() == today else at.strftime(" on %Y-%m-%d"))


def show_lines(ctx, lines):
    for line in lines:
        ctx.say(*line.render())


def left_open(survey, plan, kind):
    """What happens to an open session of `kind` if it's left open."""
    if kind == "claude":
        return "Claude Code needs /reload-plugins in each"
    if kind == "codex":
        restarts = any(s.id == "codex-daemon" and s.runs for s in plan.steps)
        return ("Codex terminals reconnect after the daemon restart" if restarts else
                "Codex terminals load kaiusap's changes once restarted")
    if survey.opencode["provider"] == 1:
        return "OpenCode won't load kaiusap until you restart it"
    return "the OpenCode terminal reconnects, but runs kaiusap's new terminal part only once restarted"


def before(ctx, survey, plan, found, upgrade_from=None):
    """What is open now, by kind, and what happens to each kind if left open."""
    if not sessions.total(found):
        return
    ctx.say("", "⚠ BEFORE PROCEEDING, CLOSE ALL SESSIONS (recommended):",
            f"  Open now: {sessions.describe(found, survey)}",
            "  → Let any running turn finish, then close each session in its own window.",
            "  → Your conversations are kept. You'll reopen them at the end.")
    if upgrade_from:
        ctx.say(f"  If you leave them open, each keeps running {upgrade_from} until you restart it.")
        return
    shown = [left_open(survey, plan, kind) for kind in ("claude", "codex", "opencode") if found.get(kind)]
    if shown:
        ctx.say("  If you leave them open: " + shown[0] + (";" if len(shown) > 1 else "."),
                *[f"  {text}{';' if i < len(shown) - 1 else '.'}" for i, text in enumerate(shown[1:], 1)])


def show_plan(ctx, steps, offset, header, extra=()):
    ctx.say("", header)
    for i, step in enumerate(steps, offset + 1):
        if step.interrupts:
            ctx.say(f" {i:>2}  ⚠ {step.title}" + (f"   runs: {step.runs}" if step.runs else ""))
            ctx.say(*[f"       {line}" for line in step.effects])
        else:
            ctx.say(f" {i:>2}  {step.title}")
        ctx.say(*[f"       {line}" for line in step.details])
        ctx.say(*[f"       → {line}" for line in step.notes])
    for line in extra:
        ctx.say(f"  {line}")


def show_left(ctx, entries):
    """The plan's closing notes, shown before the question too, so --plan and the yes cover them."""
    if not entries:
        return
    ctx.say("", "I'll leave these for you (they're in the closing list too):")
    for first, *more in entries:
        ctx.say(f"  -  {first}", *[f"     {line}" for line in more])


def changes_header(n, resumed=False):
    if resumed:
        return f"{n} change{'s' if n != 1 else ''} left:"
    return f"This will make {n} change{'s' if n != 1 else ''}:"


def confirm(ctx, opts, again="./install.sh"):
    """`Proceed? [y/N]`, asked once. --plan and no terminal change nothing."""
    if opts.plan:
        ctx.say("", "Nothing was changed (--plan).")
        return False
    if not ctx.interactive:
        ctx.say("", f"There's no terminal to ask on, so nothing was changed. Run {again} in a terminal to make "
                "these changes.")
        return False
    try:
        answer = ctx.ask("\nProceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        ctx.say("Nothing was changed.")
        return False
    return True


def wait_for_sessions(ctx, survey, first):
    """After the yes: count again; while any is open, name them and wait, unless the user goes ahead.
    Returns what was left open."""
    if not sessions.total(first):
        return {}
    while True:
        ctx.say("", "Checking again for open sessions…")
        found = sessions.count(ctx, survey)
        if not sessions.total(found):
            ctx.say("  ✓ No sessions open.")
            return {}
        it = "it" if sessions.total(found) == 1 else "them"
        ctx.say(f"  ! Still open: {sessions.describe(found, survey)}")
        try:
            answer = ctx.ask(f'  Close {it}, then press Enter to check again, or type "continue" to go ahead with '
                             f'{it} open: ')
        except EOFError:
            raise Stop("Stopped. Nothing was changed.") from None
        if answer.strip().lower() == "continue":
            return found


@contextmanager
def interrupt_guard(ctx):
    """While changes run, Ctrl-C asks to stop after the change in progress. The real runner starts
    each command in its own session, so the terminal's interrupt reaches only the installer."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        ctx.stop_requested = True
        ctx.say("  ^C  stopping once this change finishes…")

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def run_steps(ctx, steps, offset, total, on_done):
    """Each change in order; the record is written as each finishes. Returns (outcome, last number, failure)."""
    with interrupt_guard(ctx):
        for i, step in enumerate(steps, offset + 1):
            try:
                kind, rec, lines = step.action(ctx)
            except StepFailed as exc:
                return "failed", i, exc
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return "failed", i, StepFailed(f"{type(exc).__name__}: {exc}")
            on_done(step, kind, rec)
            ctx.say(f"  ✓ {i:<2} {step.done}", *[f"       {line}" for line in lines])
            if ctx.stop_requested and i < total:
                return "stopped", i, None
    return "finished", offset + len(steps), None


def report_stop(ctx, outcome, number, total, failure, again, undo):
    """A failure or a Ctrl-C: what was done, and how to finish or undo it."""
    if outcome == "failed":
        ctx.say(f"  ✗ {number:<2} {failure.reason}")
        tail = [line for line in failure.output.strip().splitlines() if line.strip()][-10:]
        ctx.say(*[f"       {line}" for line in tail])
        made = number - 1
        ctx.say("", f"Stopped at change {number} of {total}: it failed, so nothing after it was made.")
        if made and undo:
            ctx.say(f"Fix the cause, then run {again} to finish, or {undo} to undo the {made} made.")
        elif made:
            ctx.say(f"Fix the cause, then run {again} to finish.")
        else:
            ctx.say(f"Fix the cause, then run {again}.")
        return 1
    ctx.say("", f"Stopped after change {number} of {total}. Each change either finished or wasn't started.",
            f"Run {again} again to finish, or {undo} to undo these {number}." if undo else f"Run {again} again to finish.")
    return 130


def entry(ctx, step, kind, rec, run):
    return {"step": step.id, "description": step.title, "kind": kind, "record": rec, **step.meta,
            "at": ctx.iso_now(), "run": run}


def verify(ctx, survey, plan):
    """The installed `kaiusap setup`, shown. Returns its items, or None when it didn't run."""
    ctx.say("", "Verifying: kaiusap setup")
    argv = [ctx.kaiusap_bin, "setup", "--json"]
    if survey.opencode:
        argv += ["--opencode", str(survey.opencode["provider"])]
        if survey.opts.opencode_command:
            argv += ["--opencode-command", survey.opts.opencode_command]
        if survey.opts.opencode_config:
            argv += ["--opencode-config", survey.opts.opencode_config]
    try:
        code, out = ctx.run(argv, VERIFY_LIMIT)
        items = json.loads(out)["items"] if code == 0 else None
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        ctx.say(f"  ✗ kaiusap setup didn't run ({type(exc).__name__})")
        return None
    if not isinstance(items, list):
        ctx.say(f"  ✗ kaiusap setup exited {code}")
        return None
    for item in items:
        name = item.get("item")
        if item.get("done"):
            ctx.say(f"  ✓ {name}")
        elif name == "Codex hook approval":
            ctx.say(f"  → {name:<22} waiting on you, below")
        elif name == "tmux server":
            ctx.say(f"  ! {name}   {item.get('detail') or ''}".rstrip())
        else:
            ctx.say(f"  ✗ {name}   {item.get('detail') or ''}".rstrip())
    return items


def sessions_back(survey, found=None):
    """How to bring back each kind of session that was open, or None when none was (a kind that couldn't be counted
    is listed: it may have been open)."""
    def was_open(kind):
        return found is None or found.get(kind, 1) > 0
    lines = ["Bring open sessions back. kaiusap sessions prints the exact command for each."]
    if survey.claude and was_open("claude"):
        lines.append("Claude Code: restart it, or run /reload-plugins in it")
    if survey.codex and was_open("codex"):
        lines += ["Codex: open terminals reconnect by themselves; for any other thread,",
                  "       cd <its folder> && codex resume <thread id>"]
    if survey.opencode and was_open("opencode"):
        if survey.opencode["provider"] == 1:
            lines.append(f"OpenCode: quit each open session, then cd <its folder> && {survey.opencode['command']} -s <id>")
        else:
            lines += ["OpenCode: open terminals reconnect by themselves; to reopen one,",
                      f"          cd <its folder> && {survey.opencode['command']} --session <id>"]
    return lines if len(lines) > 1 else None


def handles_given(ctx):
    """Whether the store already has a handle taken: a kaiusap installed before the installer (no record) whose
    sessions have names, so "say who it is" would be noise. A store it can't read counts as none."""
    try:
        from kaiusap.store import Store
        return next(iter(Store(ctx.kaiusap_home).rows("handles.jsonl")), None) is not None
    except Exception:
        return False


def setup_leftovers(ctx, items, plan):
    """`setup` items still missing after the install that only the user can fix, as closing entries."""
    out = []
    for item in items or []:
        name = item.get("item")
        if item.get("done") or name in {"Codex hook approval", "tmux server", "tmux pane titles"} | plan.left:
            continue  # a closing note from the plan already covers it
        if name == "kaiusap found by the plugins" and ctx.kaiusap_bin.exists():
            out.append([f"Put {ctx.short(ctx.bin_dir)} on your shell's PATH, so kaiusap runs there: uv tool update-shell"])
            continue
        out.append([f"kaiusap setup still reports {name} missing" + (f": {item['detail']}" if item.get("detail") else ""),
                    *(item.get("command") or "").splitlines()])
    return out


def closing(ctx, heading, entries, footer=()):
    """One list of what only the user can do."""
    entries = [e for e in entries if e]
    n = len(entries)
    count = NUMBERS[n] if n < len(NUMBERS) else str(n)
    if not n:
        ctx.say("", f"{heading}. Nothing is left for you to do.")
    else:
        ctx.say("", f"{heading}. {count} thing{'s' if n != 1 else ''} only you can do:")
        for i, (first, *more) in enumerate(entries, 1):
            ctx.say(f"  {i}  {first}", *[f"     {line}" for line in more])
    if footer:
        ctx.say("", *footer)


# ----- install, upgrade, resume ---------------------------------------------------------------------------------

def install(ctx, opts):
    rec = records.load(ctx)
    resumed = rec is not None and not records.finished(rec)
    survey = checks.Survey(ctx, opts)
    checkout = survey.checkout
    ctx.say(f"kAIusap installer {checkout['version']}")
    mode, upgrade_from = "install", None
    if resumed:
        done = records.run_changes(rec)
        at = done[-1]["at"] if done else rec.get("started_at")
        what = "an upgrade" if rec.get("previous") else "an install"
        ctx.say(f"You stopped {what} at {when(ctx, at)} after {len(done)} of {rec.get('planned') or len(done)} "
                "changes. Picking up from there.")
        if rec.get("previous"):
            mode, upgrade_from = "upgrade", checks.label(rec["previous"])
    elif records.finished(rec):
        old = {"version": rec.get("version"), "commit": rec.get("commit"), "commit_date": rec.get("commit_date"),
               "dirty": rec.get("dirty")}
        if checks.same_build(old, checkout):
            ctx.say(f"kaiusap {checks.label(checkout)} was installed from this checkout. Checking that every piece "
                    "is in place." if not survey.current else
                    f"kaiusap {checks.label(checkout)} is installed, the same as this checkout. Checking that every "
                    "piece is in place.")
        else:
            mode, upgrade_from = "upgrade", checks.label(old)
            ctx.say(f"kaiusap {checks.label(old)} is installed. This upgrades it to {checks.label(checkout)}.")
    ctx.say("", "Checking this machine again. Nothing is changed yet." if resumed else
            "Checking this machine. Nothing is changed yet.")
    show_lines(ctx, survey.lines)
    if survey.problems:
        n = survey.problems
        ctx.say("", f"{n} problem{'s' if n != 1 else ''}. Nothing was changed.", "Fix them, then run ./install.sh again.")
        return 1
    checks.stop_without_provider(survey)
    plan = plans.build(ctx, survey, opts, mode, rec if resumed else None)
    if resumed:
        for change in records.run_changes(rec):
            if change["step"] in plan.present:
                ctx.say(f"  ✓ {done_text(change):<38} done earlier")
    offset = len(records.run_changes(rec)) if resumed else 0
    if not plan.steps:
        ctx.say("", "Everything is in place. Nothing to change.")
        if resumed:
            rec["finished_at"] = ctx.iso_now()
            records.save(ctx, rec)
        if plan.closing:
            closing(ctx, "Nothing to install", plan.closing)
        return 0
    found = sessions.count(ctx, survey)
    before(ctx, survey, plan, found, upgrade_from if mode == "upgrade" else None)
    unchanged = [f"No change needed: {', '.join(plan.unchanged)}."] if mode == "upgrade" and plan.unchanged else []
    total = offset + len(plan.steps)
    show_plan(ctx, plan.steps, offset, changes_header(len(plan.steps), resumed), unchanged)
    show_left(ctx, plan.closing)
    if not confirm(ctx, opts):
        return 0
    left = wait_for_sessions(ctx, survey, found)
    if resumed:
        run_record = rec
    elif mode == "upgrade":
        old_store = survey.store[0] if survey.store else None
        previous = {"version": rec["version"], "commit": rec.get("commit"), "commit_date": rec.get("commit_date"),
                    "dirty": rec.get("dirty"),
                    "store_backup": None, "store_format": old_store, "run": rec.get("run"),
                    "started_at": rec.get("started_at"), "finished_at": rec.get("finished_at"),
                    "planned": rec.get("planned"), "upgraded_at": None, "upgrade_run": None}
        run_record = records.new(ctx, checkout, ctx.repo, previous, rec["changes"])
        previous["upgraded_at"], previous["upgrade_run"] = run_record["started_at"], run_record["run"]
    else:
        run_record = records.new(ctx, checkout, ctx.repo, rec.get("previous") if rec else None,
                                 rec["changes"] if rec else ())
    run_record["planned"] = total
    run = run_record["run"]

    def on_done(step, kind, result):
        run_record["changes"].append(entry(ctx, step, kind, result, run))
        if step.id == "store-backup" and run_record.get("previous") and mode == "upgrade":
            run_record["previous"]["store_backup"] = result["backup"]
        records.save(ctx, run_record)

    ctx.say("")
    outcome, number, failure = run_steps(ctx, plan.steps, offset, total, on_done)
    if outcome != "finished":
        return report_stop(ctx, outcome, number, total, failure, "./install.sh", "./install.sh --uninstall")
    run_record["finished_at"] = ctx.iso_now()
    records.save(ctx, run_record)
    items = verify(ctx, survey, plan)
    entries = []
    approval = survey.codex and (any(i.get("item") == "Codex hook approval" and not i.get("done") for i in items or [])
                                 or (items is None and any(s.id == "codex-plugin" for s in plan.steps)))
    if approval:
        entries.append(["Start Codex and choose Review hooks or Trust all",
                        "Codex asks once for kaiusap's three hooks; only Codex can record that."])
    if mode == "upgrade":
        reopen = ["Reopen your sessions. kaiusap sessions prints the exact command for each."]
        if sessions.total(left):
            reopen.append(f"Still running {upgrade_from}: {sessions.describe(left, survey)}; restart each to load "
                          f"{checkout['version']}.")
        entries.append(reopen)
    else:
        entries.append(sessions_back(survey, found))
        if not records.finished(rec) and not (rec or {}).get("previous") and not handles_given(ctx):
            names = ", ".join(f'"you are {NAME_EXAMPLES[p]}"' for p in survey.providers)
            entries.append([f"In each session, say who it is: {names}"])
    entries += plan.closing
    entries += setup_leftovers(ctx, items, plan)
    if mode == "upgrade":
        closing(ctx, f"Upgraded to {checkout['version']}", entries, ["If something's wrong: ./install.sh --rollback"])
    else:
        closing(ctx, "Installed", entries, ["Check any time with: kaiusap setup", "Undo with: ./install.sh --uninstall"])
    return 0


def done_text(change):
    """A change from an earlier run, as the check names it."""
    names = {"kaiusap": "kaiusap installed", "claude-marketplace": "kaiusap marketplace in Claude Code",
             "claude-plugin": "kaiusap@kaiusap in Claude Code", "claude-enable": "kaiusap@kaiusap enabled in Claude Code",
             "codex-marketplace": "kaiusap marketplace in Codex", "codex-plugin": "kaiusap@kaiusap in Codex",
             "codex-title": "thread-id in Codex's terminal title", "opencode-link": "OpenCode plugin linked",
             "opencode-mcp": "kaiusap's MCP server in OpenCode's config", "opencode2-command": "opencode2_command set",
             "store-backup": "store backed up", "store-migrate": "store format updated"}
    return names.get(change["step"], change.get("description") or change["step"])


# ----- uninstall --------------------------------------------------------------------------------------------------

UNDOES = {"undo-claude": {"claude-plugin", "claude-marketplace", "claude-enable"},
          "undo-codex": {"codex-plugin", "codex-marketplace"}, "undo-codex-title": {"codex-title"},
          "undo-opencode-mcp": {"opencode-mcp"}, "undo-opencode2-command": {"opencode2-command"},
          "undo-opencode-link": {"opencode-link"}, "undo-kaiusap": {"kaiusap"}}


# What the installer adds, removed by hand, for an uninstall with no record to go by.
BY_HAND = """To remove kaiusap by hand, run the lines for the tools you use, in this order:
  claude plugin uninstall kaiusap@kaiusap
  claude plugin marketplace remove kaiusap
  codex plugin remove kaiusap@kaiusap
  codex plugin marketplace remove kaiusap
  codex app-server daemon restart
  rm ~/.config/opencode/plugin/kaiusap.ts      (OpenCode 1)
  rm ~/.config/opencode/plugins/kaiusap        (OpenCode 2)
  uv tool uninstall kaiusap
Then remove these by hand:
  "thread-id" from [tui] terminal_title in ~/.codex/config.toml
  the kaiusap@kaiusap entries under hooks.state in ~/.codex/config.toml
  mcp.kaiusap (OpenCode 1) or mcp.servers.kaiusap (OpenCode 2) from ~/.config/opencode/opencode.json
~/.kaiusap/ holds every message and record; delete it only if you want them gone."""


def uninstall(ctx, opts):
    rec = records.load(ctx)
    checkout = checks.checkout_build(ctx)
    ctx.say(f"kAIusap uninstaller {checkout['version']}")
    if rec is None:
        raise Stop(f"There's no install record ({ctx.short(records.path(ctx))}), so I can't tell what the installer "
                   "added. Nothing was changed.\n" + BY_HAND)
    survey = checks.Survey(ctx, opts, requirements=False)
    plan = plans.uninstall(ctx, survey, rec)
    found = sessions.count(ctx, survey)
    before(ctx, survey, plan, found)
    store_line = STORE_ASK.format(home=ctx.short(ctx.kaiusap_home))
    total = len(plan.steps) + 1
    show_plan(ctx, plan.steps, 0, changes_header(total))
    ctx.say(f" {total:>2}  {store_line}")
    if not confirm(ctx, opts, "./install.sh --uninstall"):
        return 0
    wait_for_sessions(ctx, survey, found)
    rec.setdefault("uninstall", {"started_at": ctx.iso_now(), "done": []})

    def on_done(step, kind, result):
        rec["uninstall"]["done"].append(step.id)
        for change in rec["changes"]:
            if change["step"] in UNDOES.get(step.id, ()) and not change.get("undone_at"):
                change["undone_at"] = ctx.iso_now()
        records.save(ctx, rec)

    ctx.say("")
    outcome, number, failure = run_steps(ctx, plan.steps, 0, total, on_done)
    if outcome != "finished":
        return report_stop(ctx, outcome, number, total, failure, "./install.sh --uninstall", None)
    ctx.say(f"  ? {ctx.short(ctx.kaiusap_home)}/ holds every message and record. Deleting it can't be undone.")
    try:
        answer = ctx.ask("     Delete it? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""  # the default: kept
    if answer.strip().lower() in {"y", "yes"}:
        shutil.rmtree(ctx.kaiusap_home)
        ctx.say(f"  ✓ {total:<2} Deleted {ctx.short(ctx.kaiusap_home)}/")
    else:
        for change in rec["changes"]:
            change.setdefault("undone_at", ctx.iso_now())
        rec["uninstall"]["finished_at"] = ctx.iso_now()
        records.save(ctx, rec)
        # The next ./install.sh is a fresh install; the record is kept beside it, renamed.
        os.replace(records.path(ctx), records.path(ctx).with_name(f"install.uninstalled-{ctx.stamp}.json"))
        ctx.say(f"  ✓ {total:<2} Kept {ctx.short(ctx.kaiusap_home)}/")
    entries = []
    ids = {s.id for s in plan.steps}
    if "undo-claude" in ids:
        entries.append(["Restart open Claude Code sessions, or run /reload-plugins in each."])
    if "undo-opencode-link" in ids and survey.opencode:
        if survey.opencode["provider"] == 1:
            entries.append(["Quit and reopen open OpenCode sessions: each keeps kaiusap's plugin until it quits."])
        else:
            entries.append([f"Restart OpenCode's service when it suits you ({survey.opencode['command']} service "
                            "restart): it keeps kaiusap's plugin loaded until then."])
    closing(ctx, "Uninstalled", entries)
    return 0


# ----- rollback ---------------------------------------------------------------------------------------------------

@contextmanager
def worktree(ctx, commit):
    """The checkout at `commit`, in a temporary git worktree, removed after."""
    parent = Path(tempfile.mkdtemp(prefix="kaiusap-rollback-", dir=ctx.environ.get("TMPDIR") or None))
    tree = parent / "kaiusap"
    plans.run_or_fail(ctx, ["git", "-C", ctx.repo, "worktree", "add", "--detach", tree, commit])
    try:
        yield tree
    finally:
        try:
            ctx.run(["git", "-C", ctx.repo, "worktree", "remove", "--force", tree], 60)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(parent, ignore_errors=True)


def lost_messages(ctx, backup):
    """Messages in the store now that the backup doesn't hold, oldest first: (created, from, to, subject)."""
    held = {p.name for p in (backup / "messages").glob("*.json")} if (backup / "messages").is_dir() else set()
    out = []
    for path in sorted((ctx.kaiusap_home / "messages").glob("*.json")):
        if path.name.startswith(".") or path.name in held:
            continue
        try:
            message = json.loads(path.read_text())
        except (OSError, ValueError):
            message = {}
        sender = (message.get("from") or {}).get("handle") or "?"
        recipient = (message.get("to") or {}).get("handle") or "?"
        out.append((message.get("created") or "", sender, recipient, message.get("subject") or ""))
    return sorted(out)


def rollback(ctx, opts):
    rec = records.load(ctx)
    checkout = checks.checkout_build(ctx)
    ctx.say(f"kAIusap installer {checkout['version']}")
    previous = (rec or {}).get("previous")
    if not previous:
        raise Stop("There's nothing to roll back to: only an upgrade records the version it replaced, and the install "
                   "record names none. Nothing was changed.")
    if not previous.get("commit"):
        raise Stop(f"The install record names no commit for {previous.get('version')}, so I can't reinstall it. "
                   "Nothing was changed.")
    now = {"version": rec.get("version"), "commit": rec.get("commit"), "commit_date": rec.get("commit_date"),
           "dirty": rec.get("dirty")}
    ctx.say(f"kaiusap {checks.label(now)} is installed. Rolling back to {checks.label(previous)}, the version it "
            f"replaced at {when(ctx, previous.get('upgraded_at'))}.")
    survey = checks.Survey(ctx, opts)
    blocking = [line for line in survey.lines[:3] if line.mark == "bad"]  # Python, uv, rustc: needed to reinstall
    if blocking:
        show_lines(ctx, blocking)
        raise Stop(f"{len(blocking)} problem{'s' if len(blocking) != 1 else ''}. Nothing was changed.")
    state = rec.setdefault("rollback", {"started_at": ctx.iso_now(), "done": []})
    done = set(state["done"])
    steps = []
    backup = previous.get("store_backup")
    if backup and "store-restore" not in done and Path(backup).is_dir():
        steps.append(restore_step(ctx, Path(backup)))
    if "kaiusap" not in done and not checks.same_build(survey.installed, previous):
        steps.append(plans.Step("kaiusap", f"Reinstall kaiusap {previous['version']}",
                                f"kaiusap {previous['version']} installed", reinstall_action(previous["commit"])))
    codex_plugin = False
    if survey.codex:
        from kaiusap.adapters.codex import CodexAdapter
        from kaiusap.setup import read_toml
        codex_home = ctx.home / ".codex"
        codex_plugin = CodexAdapter.setup_plugin(survey.codex["setup"], codex_home,
                                                 read_toml(codex_home / "config.toml"))["done"]
    if codex_plugin and "codex-plugin" not in done:
        steps.append(plans.Step("codex-plugin", f"Refresh the Codex plugin to {previous['version']}",
                                f"Codex plugin refreshed to {previous['version']}",
                                codex_refresh_action(previous["commit"])))
    upgraded = previous.get("upgrade_run")
    for change in records.run_changes(rec, upgraded):
        if change.get("kind") == "edit" and not change.get("undone_at") and f"undo-{change['step']}" not in done:
            steps.append(plans.undo_edit_step(ctx, change))
    program_changed = {"kaiusap", "codex-plugin"} & ({s.id for s in steps} | done)
    if survey.codex and survey.codex["daemon"] and "codex-daemon" not in done and program_changed:
        steps.append(plans.Step("codex-daemon", "Restart the Codex daemon", "Codex daemon restarted",
                                plans.commands_action(["codex", "app-server", "daemon", "restart"]),
                                runs="codex app-server daemon restart", effects=plans.DAEMON_EFFECTS))
    if survey.opencode and survey.opencode["provider"] == 2 and "opencode-service" not in done and program_changed:
        command = survey.opencode["command"]
        steps.append(plans.Step("opencode-service", "Restart OpenCode's service", "OpenCode's service restarted",
                                plans.commands_action([command, "service", "restart"]), runs=f"{command} service restart",
                                effects=plans.SERVICE_EFFECTS))
    found = sessions.count(ctx, survey)
    plan = plans.Plan(steps=steps)
    before(ctx, survey, plan, found)
    offset = len(done)
    total = offset + len(steps)
    show_plan(ctx, steps, offset, changes_header(len(steps), bool(done)))
    if not confirm(ctx, opts, "./install.sh --rollback"):
        return 0
    wait_for_sessions(ctx, survey, found)

    def on_done(step, kind, result):
        state["done"].append(step.id)
        if step.id.startswith("undo-"):
            for change in records.run_changes(rec, upgraded):
                if f"undo-{change['step']}" == step.id:
                    change["undone_at"] = ctx.iso_now()
        records.save(ctx, rec)

    ctx.say("")
    outcome, number, failure = run_steps(ctx, steps, offset, total, on_done)
    if outcome != "finished":
        return report_stop(ctx, outcome, number, total, failure, "./install.sh --rollback", None)
    # One step back: the record is the one the upgrade replaced, without the upgrade's changes.
    final = {**rec, "version": previous["version"], "commit": previous["commit"], "dirty": previous.get("dirty"),
             "run": previous.get("run") or rec.get("run"), "started_at": previous.get("started_at") or rec["started_at"],
             "finished_at": previous.get("finished_at") or ctx.iso_now(), "planned": previous.get("planned"),
             "previous": None, "changes": [c for c in rec["changes"] if c.get("run") != upgraded]}
    final.pop("rollback", None)
    records.save(ctx, final)
    entries = []
    if survey.codex:
        entries.append(["Start Codex and approve kaiusap's hooks again, if it asks"])
    entries.append(["Reopen your sessions. kaiusap sessions prints the exact command for each."])
    closing(ctx, f"Rolled back to {previous['version']}", entries)
    return 0


def restore_step(ctx, backup):
    kept = ctx.kaiusap_home.with_name(f"{ctx.kaiusap_home.name}.rolled-back-{ctx.stamp}")
    lost = lost_messages(ctx, backup)
    effects = []
    if lost:
        n = len(lost)
        effects.append(f"{n} message{'s' if n != 1 else ''} since the upgrade {'aren' if n != 1 else 'isn'}'t in it "
                       f"and will be lost:")
        for created, sender, recipient, subject in lost:
            try:
                at = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
            except ValueError:
                at = ""
            effects.append(f"  {sender} → {recipient}   \"{subject}\"   {at}".rstrip())
    effects.append(f"today's store is kept as {ctx.short(kept)}")

    def action(ctx):
        os.replace(ctx.kaiusap_home, kept)
        shutil.copytree(backup, ctx.kaiusap_home, symlinks=True)
        return "store", {"restored": str(backup), "kept": str(kept)}, []

    # Effects print right under the title, then the details: the order the mock shows (from, lost mail, kept).
    return plans.Step("store-restore", "Put back your mail and records from before the upgrade",
                      "store from before the upgrade put back", action, effects=[f"from {ctx.short(backup)}", *effects],
                      warn=True)


def reinstall_action(commit):
    def action(ctx):
        with worktree(ctx, commit) as tree:
            plans.run_or_fail(ctx, ["uv", "tool", "install", "--reinstall", tree])
        return "command", {"commands": [["uv", "tool", "install", "--reinstall", f"<{commit} worktree>"]]}, []
    return action


def codex_refresh_action(commit):
    """Codex copies the plugin from its marketplace, which is the checkout: point it at the older tree for one
    `codex plugin add`, then back at the checkout."""
    def action(ctx):
        market = plans.MARKETPLACE
        with worktree(ctx, commit) as tree:
            plans.run_or_fail(ctx, ["codex", "plugin", "marketplace", "remove", market])
            on_tree = False
            try:
                plans.run_or_fail(ctx, ["codex", "plugin", "marketplace", "add", tree])
                on_tree = True
                plans.run_or_fail(ctx, ["codex", "plugin", "add", plans.PLUGIN_ID])
                plans.run_or_fail(ctx, ["codex", "plugin", "marketplace", "remove", market])
                on_tree = False
            finally:
                if on_tree:
                    ctx.run(["codex", "plugin", "marketplace", "remove", market])
                plans.run_or_fail(ctx, ["codex", "plugin", "marketplace", "add", ctx.repo])
        return "command", {"commands": [["codex", "plugin", "add", plans.PLUGIN_ID]]}, []
    return action
