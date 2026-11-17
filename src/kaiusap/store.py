"""Plain-file storage; locks protect decisions, visibility decides success."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import time

from .errors import Error, blocked
from . import storeformat


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def identifier(at=None):
    # os.urandom, not `secrets`: importing secrets (hmac, OpenSSL) costs ~4 ms of
    # a 50 ms hook budget, and every hook write names a temporary file with this.
    # Bytes from 252 up are rejected so each of the 36 characters is equally likely.
    stamp = instant(at or now()).strftime("%Y%m%dT%H%M%SZ")
    chars = []
    while len(chars) < 6:
        chars.extend(ID_ALPHABET[b % 36] for b in os.urandom(8) if b < 252)
    return stamp + "-" + "".join(chars[:6])


def decoded(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("KAIUSAP_HOME", Path.home() / ".kaiusap"))
        self.warnings = []
        self.deadline = None
        self.hook_budget = None
        self.holding = set()  # the lock files this store holds now, such as "lock"
        self._format_seen = None  # store.json's stat when it last allowed a write
        self._format_warned = False

    def writable(self):
        """Before any write but hook-errors.log's: raise NewerFormat when store.json
        says a format newer than this kaiusap knows; create store.json, at format 1, in a store that lacks it.
        A malformed store.json counts as format 1, with a warning, and is left as it is. Reads never call this."""
        path = self.root / storeformat.FILE
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._mkdir(self.root)
            storeformat.create(self.root, self.warn)
            st = os.stat(path)
        seen = (st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)
        if seen == self._format_seen:
            return
        notes = []
        storeformat.check(self.root, notes)
        if notes and not self._format_warned:
            self._format_warned = True
            self.warnings.extend(notes)
        self._format_seen = seen

    def _sync(self, fd):
        try:
            os.fsync(fd)
        except OSError as exc:
            self.warn(exc)

    def log(self, text):
        """One line in hook-errors.log; logging itself never fails a command."""
        try:
            self._mkdir(self.root)
            with (self.root / "hook-errors.log").open("a") as stream:
                stream.write(now() + " " + str(text).replace("\n", " ") + "\n")
        except OSError:
            pass

    def warn(self, exc):
        note = "Store write or flush failed after the deciding write: " + str(exc)
        if blocked(exc):
            note += ". Probably blocked by the session's sandbox; use the kaiusap tools instead."
        self.warnings.append(note)

    def _sync_dir(self, path):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                self._sync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            self.warn(exc)

    def _mkdir(self, path):
        if not path.exists():
            self._mkdir(path.parent)
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._sync_dir(path.parent)

    @contextmanager
    def locked(self, relative="lock", deadline=None):
        budget = self.hook_budget
        if budget:
            # Committed work finishes to the hard limit; otherwise the shorter of the caller's limit
            # (such as the start hook's 1 s global-lock wait) and the budget's admission deadline.
            deadline = budget.deadline if deadline is None or budget.finishing else min(deadline, budget.deadline)
        elif deadline is None:
            deadline = self.deadline
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("store deadline reached")
        self.writable()
        path = self.root / relative
        self._mkdir(path.parent)
        while True:
            fresh = not path.exists()
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                if deadline is None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                else:
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("store lock deadline reached")
                            time.sleep(min(.001, max(0, deadline - time.monotonic())))
                if not self._same_file(fd, path):
                    # The file was replaced while this call waited (an adapter's one-time move, under
                    # the same lock): lock the file now at the path, so no line lands in the old one.
                    os.close(fd)
                    continue
            except BaseException:
                os.close(fd)
                raise
            break
        try:
            if self.hook_budget:
                self.hook_budget.check()
            self.holding.add(relative)
            try:
                yield fd
            finally:
                self.holding.discard(relative)
            if fresh:
                self._sync_dir(path.parent)
        finally:
            os.close(fd)

    @staticmethod
    def _same_file(fd, path):
        try:
            here = os.stat(path)
        except FileNotFoundError:
            return False
        locked = os.fstat(fd)
        return (here.st_ino, here.st_dev) == (locked.st_ino, locked.st_dev)

    @staticmethod
    def _write(fd, data):
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise OSError("short write made no progress")
            data = data[count:]

    def _append(self, fd, record):
        size = os.lseek(fd, 0, os.SEEK_END)
        if size and os.pread(fd, 1, size - 1) != b"\n":
            self._write(fd, b"\n")
        self._write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
        if self.hook_budget and record.get("event") in {"reminded", "named"}:
            self.hook_budget.commit()
        self._sync(fd)

    def append(self, relative, record, deadline=None):
        with self.locked(relative, deadline) as fd:
            self._append(fd, record)
        if relative in {"handles.jsonl", "sessions/personas.jsonl"} or (
                relative.startswith("events/") and record.get("event") in {"read", "named", "reminded"}):
            self.changed()

    def changed(self):
        """Invalidate hook acknowledgements after the authoritative write is visible."""
        try:
            self.writable()
            from .hookstate import atomic
            atomic(self.root / "markers/changed", identifier())
        except OSError as exc:
            self.warn(exc)
            # Source fingerprints also fence a failed marker update.

    def rows(self, relative, reverse=False):
        try:
            stream = (self.root / relative).open("rb")
        except FileNotFoundError:
            return
        with stream:
            if not reverse:
                for line in stream:
                    row = decoded(line)
                    if row is not None:
                        yield row
                return
            position = stream.seek(0, os.SEEK_END)
            remainder = b""
            while position:
                size = min(position, 65536)
                position -= size
                stream.seek(position)
                pieces = (stream.read(size) + remainder).split(b"\n")
                remainder = pieces.pop(0)
                for line in reversed(pieces):
                    row = decoded(line)
                    if row is not None:
                        yield row
            row = decoded(remainder)
            if row is not None:
                yield row

    def latest(self, relative, predicate, process_started_at=None):
        return self.latest_of(self.rows(relative, reverse=True), predicate, process_started_at)

    @staticmethod
    def latest_of(rows, predicate, process_started_at=None):
        """`latest` over rows given newest first, such as an adapter's merged reading of two files."""
        best = None
        first = None
        for row in rows:
            try:
                at = instant(row["at"])
            except (KeyError, ValueError, TypeError):
                continue
            if process_started_at and at < instant(process_started_at):
                break
            if first is not None and at < first - timedelta(seconds=3):
                break
            if predicate(row):
                if first is None:
                    first = at
                if best is None or at > instant(best["at"]):
                    best = row
        return best

    def event(self, event, *, deadline=None, **fields):
        record = {"at": now(), "event": event, **fields}
        month = instant(record["at"]).astimezone(timezone.utc).strftime("%Y-%m")
        self.append(f"events/{month}.jsonl", record, deadline)
        return record

    def events(self, since=None):
        month = instant(since).astimezone(timezone.utc).strftime("%Y-%m") if since else ""
        for path in sorted((self.root / "events").glob("????-??.jsonl")):
            if path.stem >= month:
                yield from self.rows(path.relative_to(self.root))

    def messages(self, since=None):
        cutoff = instant(since).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") if since else ""
        for path in sorted((self.root / "messages").glob("*.json")):
            if not path.name.startswith(".") and path.stem >= cutoff:
                yield json.loads(path.read_text())

    def message(self, prefix):
        matches = [p for p in (self.root / "messages").glob("*.json")
                   if not p.name.startswith(".") and p.stem.startswith(prefix)]
        if not matches:
            raise Error("Unknown message. Check kaiusap inbox for its id.", 3)
        if len(matches) > 1:
            raise Error("Ambiguous message id: " + ", ".join(sorted(p.stem for p in matches)) + ". Use a longer id.", 2)
        return json.loads(matches[0].read_text())

    def save_message(self, message):
        self.writable()
        directory = self.root / "messages"
        self._mkdir(directory)
        for path in directory.glob(".tmp-*"):
            try:
                if path.stat().st_mtime < time.time() - 3600:
                    path.unlink()
            except FileNotFoundError:
                pass
        while True:
            mid = message["id"]
            temp = directory / (".tmp-" + mid)
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self._redraw(message)
                continue
            try:
                try:
                    self._write(fd, (json.dumps(message, indent=2, ensure_ascii=False) + "\n").encode())
                    self._sync(fd)
                finally:
                    os.close(fd)
                try:
                    os.link(temp, directory / (mid + ".json"))
                except FileExistsError:
                    self._redraw(message)
                    continue
                self._sync_dir(directory)
                self.changed()
                return
            finally:
                try:
                    temp.unlink()
                except OSError as exc:
                    self.warn(exc)

    @staticmethod
    def _redraw(message):
        old = message["id"]
        message["id"] = identifier(message["created"])
        if message["chain_id"] == old:
            message["chain_id"] = message["id"]

    def set_paused(self, paused):
        self.writable()
        self._mkdir(self.root)
        path = self.root / "paused"
        if paused:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            self._sync(fd)
            os.close(fd)
        else:
            path.unlink(missing_ok=True)
        self._sync_dir(self.root)
        self.changed()

    @property
    def paused(self):
        return (self.root / "paused").exists()

    def adapter_dir(self, provider):
        """The adapter's own directory in the store, for the files it owns, such as a transcript
        index or a terminal map: `sessions/<provider>/`."""
        return self.root / "sessions" / provider

    def write_json(self, path, value):
        """Replace a small derived file in one write: a temporary file, synced, renamed over it."""
        self.writable()
        self._mkdir(path.parent)
        temp = path.with_name(".tmp-" + identifier())
        try:
            with temp.open("x") as stream:
                json.dump(value, stream)
                stream.flush()
                self._sync(stream.fileno())
            os.replace(temp, path)
            self._sync_dir(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    def start(self, record, deadline=None, then=None):
        """Append a start (or cwd) record. `then(record)`, when given, runs under the same append lock right
        after it: the adapter's update of a file it derives from its records; its OSError is a warning."""
        with self.locked("sessions/starts.jsonl", deadline) as fd:
            self._append(fd, record)
            if then:
                try:
                    then(record)
                except OSError as exc:
                    self.warn(exc)
