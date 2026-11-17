//! The native hook engine: reads and checks the hook input under its deadline, appends
//! records under the store's lock, runs commands under the deadline, logs hook errors, checks the
//! store's signature, and hands over to Python. It names no provider: each provider's fast path is
//! an optional part, `src/kaiusap/adapters/<p>.rs`, behind `Provider` below, and the build
//! generates the table of parts (`PARTS`) from the files it finds.
//! Non-hook commands exec the Python interpreter beside this installed executable.
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::process::CommandExt;
use std::process::Command;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// A dependency-free JSON validator for the hook envelope. No store is opened
// for valid inputs; in particular an unrelated Bash call returns immediately.
struct Json<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl Json<'_> {
    fn space(&mut self) {
        while self.pos < self.bytes.len()
            && matches!(self.bytes[self.pos], b' ' | b'\n' | b'\r' | b'\t')
        {
            self.pos += 1;
        }
    }
    fn take(&mut self, byte: u8) -> bool {
        if self.bytes.get(self.pos) == Some(&byte) {
            self.pos += 1;
            true
        } else {
            false
        }
    }
    fn string(&mut self) -> bool {
        if !self.take(b'"') {
            return false;
        }
        while let Some(&byte) = self.bytes.get(self.pos) {
            self.pos += 1;
            match byte {
                b'"' => return true,
                0..=31 => return false,
                b'\\' => {
                    let Some(&escaped) = self.bytes.get(self.pos) else {
                        return false;
                    };
                    self.pos += 1;
                    match escaped {
                        b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => (),
                        b'u' => {
                            for _ in 0..4 {
                                if !self.bytes.get(self.pos).is_some_and(u8::is_ascii_hexdigit) {
                                    return false;
                                }
                                self.pos += 1;
                            }
                        }
                        _ => return false,
                    }
                }
                _ => (),
            }
        }
        false
    }
    fn digits(&mut self) -> bool {
        let start = self.pos;
        while self.bytes.get(self.pos).is_some_and(u8::is_ascii_digit) {
            self.pos += 1;
        }
        self.pos > start
    }
    fn number(&mut self) -> bool {
        self.take(b'-');
        if !self.take(b'0') && !self.digits() {
            return false;
        }
        if self.take(b'.') && !self.digits() {
            return false;
        }
        if self.take(b'e') || self.take(b'E') {
            if !self.take(b'+') {
                self.take(b'-');
            }
            if !self.digits() {
                return false;
            }
        }
        true
    }
    fn value(&mut self, depth: usize) -> bool {
        if depth > 128 {
            return false;
        }
        self.space();
        match self.bytes.get(self.pos).copied() {
            Some(b'"') => self.string(),
            Some(b'{') => {
                self.pos += 1;
                self.space();
                if self.take(b'}') {
                    return true;
                }
                loop {
                    self.space();
                    if !self.string() {
                        return false;
                    }
                    self.space();
                    if !self.take(b':') || !self.value(depth + 1) {
                        return false;
                    }
                    self.space();
                    if self.take(b'}') {
                        return true;
                    }
                    if !self.take(b',') {
                        return false;
                    }
                }
            }
            Some(b'[') => {
                self.pos += 1;
                self.space();
                if self.take(b']') {
                    return true;
                }
                loop {
                    if !self.value(depth + 1) {
                        return false;
                    }
                    self.space();
                    if self.take(b']') {
                        return true;
                    }
                    if !self.take(b',') {
                        return false;
                    }
                }
            }
            Some(b't' | b'f' | b'n') => {
                let literal: &[u8] = match self.bytes[self.pos] {
                    b't' => b"true",
                    b'f' => b"false",
                    _ => b"null",
                };
                if self.bytes[self.pos..].starts_with(literal) {
                    self.pos += literal.len();
                    true
                } else {
                    false
                }
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => false,
        }
    }
}

fn envelope(input: &[u8]) -> bool {
    if std::str::from_utf8(input).is_err() {
        return false;
    }
    let mut parser = Json {
        bytes: input,
        pos: 0,
    };
    parser.space();
    if parser.bytes.get(parser.pos) != Some(&b'{') || !parser.value(0) {
        return false;
    }
    parser.space();
    parser.pos == input.len()
}

fn log_error(reason: &str) {
    let root = env::var_os("KAIUSAP_HOME")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".kaiusap"))
        });
    if let Some(root) = root {
        if fs::create_dir_all(&root).is_ok() {
            if let Ok(mut file) = OpenOptions::new()
                .create(true)
                .append(true)
                .open(root.join("hook-errors.log"))
            {
                let seconds = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs();
                let _ = writeln!(file, "{seconds} {reason}");
            }
        }
    }
}

fn fields(input: &str) -> Option<std::collections::BTreeMap<String, String>> {
    let mut p = Json {
        bytes: input.as_bytes(),
        pos: 0,
    };
    p.space();
    if !p.take(b'{') {
        return None;
    }
    let mut out = std::collections::BTreeMap::new();
    p.space();
    if p.take(b'}') {
        return Some(out);
    }
    loop {
        p.space();
        let a = p.pos;
        if !p.string() {
            return None;
        }
        let key = string(&input[a..p.pos])?;
        p.space();
        if !p.take(b':') {
            return None;
        }
        p.space();
        let a = p.pos;
        if !p.value(0) {
            return None;
        }
        if out.insert(key, input[a..p.pos].to_string()).is_some() {
            return None;
        }
        p.space();
        if p.take(b'}') {
            break;
        }
        if !p.take(b',') {
            return None;
        }
    }
    p.space();
    if p.pos != input.len() {
        return None;
    }
    Some(out)
}
fn string(raw: &str) -> Option<String> {
    let mut chars = raw.strip_prefix('"')?.strip_suffix('"')?.chars();
    let mut out = String::new();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        out.push(match chars.next()? {
            '"' => '"',
            '\\' => '\\',
            '/' => '/',
            'n' => '\n',
            'r' => '\r',
            't' => '\t',
            'b' => '\u{8}',
            'f' => '\u{c}',
            'u' => {
                let digits: String = chars.by_ref().take(4).collect();
                char::from_u32(u32::from_str_radix(&digits, 16).ok()?)?
            }
            _ => return None,
        });
    }
    Some(out)
}
fn quote(value: &str) -> String {
    let mut out = String::from("\"");
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if c < ' ' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}
fn get(m: &std::collections::BTreeMap<String, String>, key: &str) -> Option<String> {
    string(m.get(key)?)
}
fn root() -> std::path::PathBuf {
    env::var_os("KAIUSAP_HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::PathBuf::from(env::var_os("HOME").unwrap_or_default()).join(".kaiusap")
        })
}
fn signature(root: &std::path::Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    let mut paths: Vec<String> = [
        "handles.jsonl",
        "sessions/personas.jsonl",
        "messages",
        "settings.json",
        "paused",
        "markers/changed",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let mut events = Vec::new();
    match fs::read_dir(root.join("events")) {
        Ok(entries) => {
            for entry in entries {
                let name = entry.ok()?.file_name().into_string().ok()?;
                if name.len() == 13 && name.ends_with(".jsonl") && name.as_bytes()[4] == b'-' {
                    events.push(format!("events/{name}"));
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(_) => return None,
    }
    events.sort();
    paths.extend(events);
    let mut result = Vec::new();
    for path in paths {
        match fs::metadata(root.join(&path)) {
            Ok(m) => result.push(format!(
                "{}:{}:{}:{}",
                path,
                m.ino(),
                m.len(),
                m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128
            )),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => result.push(format!("{path}:-")),
            Err(_) => return None,
        }
    }
    Some(result.join("\n"))
}
// Civil date conversion from Unix days, using the proleptic Gregorian calendar.
fn timestamp() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = elapsed.as_secs() as i64;
    let z = secs / 86400 + 719468;
    let era = z / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    y += if m <= 2 { 1 } else { 0 };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{:06}Z",
        secs % 86400 / 3600,
        secs % 3600 / 60,
        secs % 60,
        elapsed.subsec_micros()
    )
}
extern "C" {
    fn flock(fd: i32, op: i32) -> i32;
    fn kill(pid: i32, sig: i32) -> i32;
    fn fsync(fd: i32) -> i32;
}
/// The store format this engine writes: Python's `storeformat.STORE_FORMAT`.
const STORE_FORMAT: i64 = 1;
/// Why a store refuses native appends: store.json names a format newer than STORE_FORMAT. A missing or
/// malformed store.json is format 1, as in Python, and the engine never writes store.json itself.
fn newer_format(root: &std::path::Path) -> Option<String> {
    let store = fields(&small_text(&root.join("store.json"), 65536)?)?;
    let found: i64 = store.get("format")?.parse().ok()?;
    if found <= STORE_FORMAT {
        return None;
    }
    Some(match get(&store, "written_by").filter(|by| !by.is_empty()) {
        Some(by) => format!(
            "This store is format {found}, written by kaiusap {by}; this kaiusap only knows format {STORE_FORMAT}. Install kaiusap {by} or later."
        ),
        None => format!(
            "This store is format {found}, written by a newer kaiusap; this kaiusap only knows format {STORE_FORMAT}. Install a newer kaiusap."
        ),
    })
}
// Every native append syncs as Python's store does (plain fsync), not with sync_all,
// which is F_FULLFSYNC on macOS (~4 ms) and pushed the 50 ms hooks past their budget.
// As the Python store does, it appends only to the file at the path once it holds the lock: a file
// replaced while it waited (a one-time move under that lock) is reopened, so no line lands in it.
fn append(
    root: &std::path::Path,
    relative: &str,
    line: &str,
    end: Instant,
) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{FileExt, OpenOptionsExt};
    if let Some(reason) = newer_format(root) {
        return Err(std::io::Error::new(std::io::ErrorKind::Other, reason));
    }
    let path = root.join(relative);
    let parent = path.parent().unwrap();
    fs::create_dir_all(parent)?;
    let (fresh, mut f) = loop {
        let fresh = !path.exists();
        let f = OpenOptions::new()
            .create(true)
            .read(true)
            .append(true)
            .mode(0o600)
            .open(&path)?;
        loop {
            if Instant::now() >= end {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "append deadline",
                ));
            }
            if unsafe { flock(f.as_raw_fd(), 2 | 4) } == 0 {
                break;
            }
            let e = std::io::Error::last_os_error();
            if e.kind() != std::io::ErrorKind::WouldBlock {
                return Err(e);
            }
            std::thread::sleep(std::time::Duration::from_micros(250));
        }
        if same_file(&f, &path)? {
            break (fresh, f);
        }
    };
    let len = f.metadata()?.len();
    if len > 0 {
        let mut byte = [0];
        f.read_at(&mut byte, len - 1)?;
        if byte != [b'\n'] {
            f.write_all(b"\n")?;
        }
    }
    f.write_all(format!("{line}\n").as_bytes())?;
    let sync = |file: &fs::File| -> std::io::Result<()> {
        if unsafe { fsync(file.as_raw_fd()) } == 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    };
    sync(&f)?;
    if fresh {
        sync(&fs::File::open(parent)?)?;
    }
    Ok(())
}
// Whether an open file is still the one at the path (its device and inode).
fn same_file(file: &fs::File, path: &std::path::Path) -> std::io::Result<bool> {
    use std::os::unix::fs::MetadataExt;
    let open = file.metadata()?;
    match fs::metadata(path) {
        Ok(here) => Ok(here.ino() == open.ino() && here.dev() == open.dev()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}
fn run(mut command: Command, input: Option<Vec<u8>>, end: Instant) -> Option<(bool, Vec<u8>)> {
    use std::process::Stdio;
    if Instant::now() >= end {
        return None;
    }
    let own_group = env::var_os("KAIUSAP_RECORD_ONLY").is_none();
    if own_group {
        command.process_group(0);
    }
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| log_error(&format!("hook subprocess spawn failed: {e}")))
        .ok()?;
    let mut stdin = child.stdin.take()?;
    let _writer = std::thread::spawn(move || {
        if let Some(data) = input {
            let _ = stdin.write_all(&data);
        }
    });
    let mut stdout = child.stdout.take()?;
    let (send_output, receive_output) = std::sync::mpsc::channel();
    let _reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stdout.read_to_end(&mut bytes);
        let _ = send_output.send(bytes);
    });
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status.success()),
            Ok(None) => (),
            Err(_) => break None,
        }
        if Instant::now() >= end {
            if own_group {
                unsafe {
                    kill(-(child.id() as i32), 9);
                }
            } else {
                let _ = child.kill();
            }
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(std::time::Duration::from_micros(250));
    };
    // A command can exit while a descendant still holds its pipe open.
    // Reap the private group and bound output collection by the same deadline.
    if own_group {
        unsafe {
            kill(-(child.id() as i32), 9);
        }
    }
    let output = receive_output
        .recv_timeout(end.saturating_duration_since(Instant::now()))
        .ok()?;
    status.map(|success| (success, output))
}
fn python(
    args: &[std::ffi::OsString],
    input: Vec<u8>,
    end: Instant,
    hard_end: Instant,
    at: &str,
    recorded: bool,
    started: Instant,
    refusal: Option<String>,
) {
    // A screen's hand-over fails closed: when Python gives no valid output by the deadline, or
    // fails, the part's refusal is printed instead of empty output.
    let refuse = |reason: &str| {
        log_error(reason);
        if let Some(text) = &refusal {
            let _ = std::io::stdout().write_all(format!("{text}\n").as_bytes());
        }
    };
    if Instant::now() >= end {
        refuse("hook admission deadline reached before Python");
        return;
    }
    let Some(exe) = env::current_exe()
        .ok()
        .and_then(|p| fs::canonicalize(p).ok())
    else {
        refuse("cannot locate hook Python");
        return;
    };
    let mut command = Command::new(exe.parent().unwrap().join("python"));
    let now = Instant::now();
    let wall_now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64();
    let wall_end = wall_now + end.saturating_duration_since(now).as_secs_f64();
    let wall_hard_end = wall_now + hard_end.saturating_duration_since(now).as_secs_f64();
    // The hook's start, for Python's own budget when no part could say which hook this is.
    let wall_start = wall_now - started.elapsed().as_secs_f64();
    command
        .arg("-m")
        .arg("kaiusap")
        .arg("hook")
        .args(args)
        .env("KAIUSAP_HOOK_END", wall_end.to_string())
        .env("KAIUSAP_HOOK_HARD_END", wall_hard_end.to_string())
        .env("KAIUSAP_HOOK_AT", at)
        .env("KAIUSAP_HOOK_START", wall_start.to_string())
        .env("KAIUSAP_NATIVE_ENTRY", &exe)
        .env("KAIUSAP_NATIVE_RECORDED", if recorded { "1" } else { "0" });
    // Python stops admitting work at end, but committed output may finish
    // until the absolute hard limit. A hung interpreter is killed here too.
    match run(command, Some(input), hard_end) {
        // A screen's answer is never empty: Python prints its decision, `{}` when it allows the call.
        Some((true, out)) if out.is_empty() && refusal.is_some() => {
            refuse("hook Python gave a screen no output")
        }
        Some((true, out)) if out.is_empty() || envelope(out.trim_ascii()) => {
            let _ = std::io::stdout().write_all(&out);
        }
        _ => refuse("hook Python failed or hard deadline reached (1 s for constant hooks)"),
    }
}
fn safe_id(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
}
fn uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
}
fn small_text(path: &std::path::Path, limit: u64) -> Option<String> {
    let mut data = String::new();
    fs::File::open(path)
        .ok()?
        .take(limit + 1)
        .read_to_string(&mut data)
        .ok()?;
    if data.len() as u64 > limit {
        None
    } else {
        Some(data)
    }
}
// A file's fingerprint as Python's hook state writes it: inode, size and nanosecond mtime.
fn fingerprint(path: &std::path::Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    let st = fs::File::open(path).ok()?.metadata().ok()?;
    Some(format!(
        "{}:{}:{}",
        st.ino(),
        st.len(),
        st.mtime() as i128 * 1_000_000_000 + st.mtime_nsec() as i128
    ))
}

// ----- The provider interface ------------------------------------------------------------

/// A hook input's top-level fields, each as its raw JSON text.
type Fields = std::collections::BTreeMap<String, String>;

/// What a provider part decides about an input before any store work.
enum Screen {
    /// Nothing to do: return empty output, without starting Python.
    Done,
    /// Hand the input to Python, with no records written.
    Python,
    /// Go on to the records.
    Continue,
}

/// What a provider part's fixed-shape records came to.
enum Records {
    /// The input is not one the part recognises: Python decides and writes everything.
    Python,
    /// Nothing written; Python may still have work (it then writes what it decides).
    Skipped,
    /// The records are written; Python may still have work, and is told they are written.
    Appended,
    /// The records are written, or none are owed, and nothing else is left to do.
    Done,
    /// An append failed and is logged: stop, without starting Python.
    Failed,
}

/// A provider's native part. Every call uses only the engine's helpers.
trait Provider: Sync {
    /// Whether `event` is the provider's start hook (3 s, no grace) rather than a constant hook
    /// (50 ms, committed output finished within 1 s).
    fn start(&self, event: &str) -> bool;
    /// The decision before any store work, such as a spawned agent's prompt.
    fn screen(&self, _event: &str, _input: &Fields) -> Screen {
        Screen::Continue
    }
    /// The hook output to print when a screen handed to Python (`Screen::Python`) gets no valid answer by
    /// its deadline: Python missed it, exited without output, or failed. A part whose screens refuse
    /// calls gives its refusal, so an unchecked call fails closed. None prints nothing; a part
    /// without screens keeps this default.
    fn screen_timeout(&self, _event: &str, _input: &Fields) -> Option<String> {
        None
    }
    /// For one of the part's tool-call screens, known from the event name alone: the hook output to print
    /// when the input didn't arrive whole within the screen's 1 s from the hook's start, or can't be read
    /// or parsed, which is the provider's refusal for a call it couldn't check in time. Some marks the
    /// event as a screen, whose input the engine reads under that 1 s instead of the event's own deadline.
    /// With no input the caller can't be told apart, so this may refuse a root caller's call once. None,
    /// the default, is any other event.
    fn input_refusal(&self, _event: &str) -> Option<String> {
        None
    }
    /// Append the event's fixed-shape records through the engine, or ask for Python.
    fn records(&self, _event: &str, _input: &Fields, _at: &str, _engine: &Engine) -> Records {
        Records::Python
    }
    /// Whether nothing is left to do. A part checks its own input and asks the engine for the
    /// session's acknowledged marker (`Engine::acknowledged`), which carries the shared checks.
    fn clean(&self, _event: &str, _input: &Fields, _engine: &Engine) -> bool {
        false
    }
}

// The generated table: `#[path] mod <p>;` per part and `PARTS`, the name of each part with it.
include!(env!("KAIUSAP_PARTS"));

fn part(name: &std::ffi::OsStr) -> Option<(&'static str, &'static dyn Provider)> {
    PARTS
        .iter()
        .find(|(registered, _)| name == std::ffi::OsStr::new(registered))
        .copied()
}

/// What the engine lends a part for one hook call.
struct Engine<'a> {
    name: &'a str,
    end: Instant,
    decided: bool,
}

impl Engine<'_> {
    /// Python already decided this input holds no notice and asked for the records alone.
    fn decided(&self) -> bool {
        self.decided
    }
    /// Append one line to a store file under its lock, within the hook's deadline.
    fn append(&self, relative: &str, line: &str) -> std::io::Result<()> {
        append(&root(), relative, line, self.end)
    }
    /// Run a command under the hook's deadline: its success and its output, or None.
    fn run(&self, command: Command, input: Option<Vec<u8>>) -> Option<(bool, Vec<u8>)> {
        run(command, input, self.end)
    }
    /// The session's hook-state acknowledgement (hook markers), when the shared checks pass:
    /// the change token is readable, the marker is version 1 with no mail pending, and the
    /// projection and the store are unchanged since Python wrote it. Otherwise None, which is work.
    fn acknowledged(&self, sid: &str) -> Option<Fields> {
        if !safe_id(sid) {
            return None;
        }
        small_text(&root().join("markers/changed"), 256).and_then(|s| string(&s))?;
        let markers = root().join("markers").join(self.name);
        let marker = small_text(&markers.join(format!("{sid}.json")), 65536).and_then(|s| fields(&s))?;
        if marker.get("version").map(String::as_str) != Some("1") {
            return None;
        }
        if marker.get("mail_pending").map(String::as_str) != Some("false") {
            return None;
        }
        let state = fingerprint(&markers.join(format!("{sid}.state")))?;
        if get(&marker, "state_signature") != Some(state) {
            return None;
        }
        if get(&marker, "signature") != signature(&root()) {
            return None;
        }
        Some(marker)
    }
}

/// The 1 s given to committed work, from the hook's start: a constant hook's hard limit, and all of a
/// screen handed to Python.
const COMMITTED_MS: u64 = 1000;

fn hook(args: &[std::ffi::OsString]) {
    let started = Instant::now();
    let at = timestamp();
    if args.len() != 2 {
        log_error("invalid hook arguments");
        return;
    }
    let found = part(&args[0]);
    let event = args[1].to_str().unwrap_or("");
    let record_only = env::var_os("KAIUSAP_RECORD_ONLY").is_some();
    if found.is_none() && record_only {
        // Only a part writes records natively; Python asked a name without one.
        log_error("no native part for hook records");
        println!("{{\"recorded\":false}}");
        return;
    }
    // A provider without a part runs on Python hooks: the engine can't tell its start hook from
    // a constant hook, so it gives Python the start hook's deadline and Python applies its own budget.
    let start = found.map_or(true, |(_, part)| part.start(event));
    let mut end = started + std::time::Duration::from_millis(if start { 3000 } else { 50 });
    let at = if record_only {
        let remaining = env::var("KAIUSAP_HOOK_END")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(0.0)
            - SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
        if !remaining.is_finite() || remaining <= 0.0 {
            log_error("native record deadline reached");
            return;
        }
        end = end.min(Instant::now() + std::time::Duration::from_secs_f64(remaining.min(0.050)));
        env::var("KAIUSAP_HOOK_AT").unwrap_or(at)
    } else {
        at
    };
    // A constant hook's committed output may finish until 1 s; the start hook has no grace.
    let hard_end = if start {
        end
    } else {
        started + std::time::Duration::from_millis(COMMITTED_MS)
    };
    // A screen event's input is read under the screen's 1 s, and input that doesn't come whole, or can't
    // be read or parsed, gets the part's no-input refusal; every other event's input is
    // read under its own deadline, and such input ends the hook with nothing printed.
    let no_input = found
        .filter(|_| !record_only)
        .and_then(|(_, part)| part.input_refusal(event));
    let read_end = if no_input.is_some() {
        started + std::time::Duration::from_millis(COMMITTED_MS)
    } else {
        end
    };
    let refuse_input = |reason: &str| {
        log_error(reason);
        if let Some(text) = &no_input {
            let _ = std::io::stdout().write_all(format!("{text}\n").as_bytes());
        }
    };
    let (send, receive) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut input = Vec::new();
        let result = std::io::stdin().read_to_end(&mut input).map(|_| input);
        let _ = send.send(result);
    });
    let input = match receive.recv_timeout(read_end.saturating_duration_since(Instant::now())) {
        Ok(Ok(input)) if envelope(&input) => input,
        Ok(_) => {
            refuse_input("invalid hook input");
            return;
        }
        Err(_) => {
            refuse_input("hook input deadline reached");
            return;
        }
    };
    let Some((name, part)) = found else {
        python(args, input, end, hard_end, &at, false, started, None);
        return;
    };
    let Some(p) = std::str::from_utf8(&input).ok().and_then(fields) else {
        if no_input.is_some() {
            refuse_input("unrecognised hook input for a screen");
        } else {
            python(args, input, end, hard_end, &at, false, started, None);
        }
        return;
    };
    match part.screen(event, &p) {
        Screen::Done => return,
        Screen::Python => {
            // A screen handed over gets the committed-work 1 s from the hook's start, with no grace
            // beyond it; past it, or without a valid answer, the part's refusal is printed.
            let end = started + std::time::Duration::from_millis(COMMITTED_MS);
            python(args, input, end, end, &at, false, started, part.screen_timeout(event, &p));
            return;
        }
        Screen::Continue => (),
    }
    let engine = Engine {
        name,
        end,
        decided: record_only,
    };
    let result = part.records(event, &p, &at, &engine);
    if matches!(result, Records::Failed) {
        return;
    }
    let recorded = matches!(result, Records::Appended | Records::Done);
    if record_only {
        println!("{{\"recorded\":{recorded}}}");
        return;
    }
    match result {
        Records::Python => {
            python(args, input, end, hard_end, &at, false, started, None);
            return;
        }
        Records::Done => return,
        _ => (),
    }
    if part.clean(event, &p, &engine) {
        return;
    }
    python(args, input, end, hard_end, &at, recorded, started, None);
}

fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "__hook_record") {
        env::set_var("KAIUSAP_RECORD_ONLY", "1");
        hook(&args[1..]);
        return;
    }
    if args.first().is_some_and(|arg| arg == "hook") {
        hook(&args[1..]);
        return;
    }
    let executable = match env::current_exe().and_then(fs::canonicalize) {
        Ok(path) => path,
        Err(error) => {
            eprintln!("kaiusap: cannot locate installed executable: {error}");
            std::process::exit(64);
        }
    };
    let python = executable.parent().unwrap().join("python");
    let error = Command::new(python)
        .arg("-m")
        .arg("kaiusap")
        .args(args)
        .exec();
    eprintln!(
        "kaiusap: cannot start the installed Python interpreter: {error}. Reinstall kaiusap."
    );
    std::process::exit(64);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strings_and_fields() {
        let text = "café\n\t\"\\";
        assert_eq!(string(&quote(text)).as_deref(), Some(text));
        // json.dumps(ensure_ascii=False)'s escapes: by name where it has one, else \u00xx; others as themselves.
        assert_eq!(quote("\u{8}\u{c}\u{1}\u{1f}\u{7f}é/"), "\"\\b\\f\\u0001\\u001f\u{7f}é/\"");
        assert!(fields(r#"{"x":1,"x":2}"#).is_none());
        assert!(string(r#""\ud800""#).is_none());
        assert_eq!(
            get(&fields(r#"{"x":"\u0061"}"#).unwrap(), "x").as_deref(),
            Some("a")
        );
    }
    #[test]
    fn envelopes() {
        for value in [
            r#"{}"#,
            r#"{"command":"echo hello", "nested":[true,false,null,-1.2e+3], "unicode":"\u00e9"}"#,
        ] {
            assert!(envelope(value.as_bytes()));
        }
        for value in [
            "null",
            "[]",
            "{",
            "{}{}",
            "{\"x\":01}",
            "{\"x\":1.}",
            "{\"x\":true,}",
            "{\"x\":NaN}",
            "{\"x\":\"\\q\"}",
        ] {
            assert!(!envelope(value.as_bytes()), "{value}");
        }
    }
    #[test]
    fn registry_finds_exactly_the_registered_parts() {
        let mut names: Vec<_> = PARTS.iter().map(|(name, _)| *name).collect();
        names.sort();
        names.dedup();
        assert_eq!(names.len(), PARTS.len());
        for name in names {
            assert!(name.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_'));
            assert_eq!(part(std::ffi::OsStr::new(name)).map(|(n, _)| n), Some(name));
            assert!(part(std::ffi::OsStr::new(&name.to_uppercase())).is_none());
        }
        assert!(part(std::ffi::OsStr::new("")).is_none());
    }
    #[test]
    fn acknowledgement_refuses_an_unsafe_session_id() {
        let engine = Engine {
            name: "x",
            end: Instant::now(),
            decided: false,
        };
        assert!(engine.acknowledged("../x").is_none());
        assert!(engine.acknowledged("").is_none());
    }
    #[test]
    fn append_follows_a_file_replaced_while_it_waits() {
        use std::os::fd::AsRawFd;
        let dir = std::env::temp_dir().join(format!("kaiusap-append-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("f.jsonl"), b"{\"a\": 1}\n").unwrap();
        let held = OpenOptions::new().read(true).open(dir.join("f.jsonl")).unwrap();
        assert_eq!(unsafe { flock(held.as_raw_fd(), 2) }, 0);
        let root = dir.clone();
        let writer = std::thread::spawn(move || {
            append(&root, "f.jsonl", "{\"c\": 3}", Instant::now() + std::time::Duration::from_secs(5))
        });
        std::thread::sleep(std::time::Duration::from_millis(30));
        fs::write(dir.join("g"), b"{\"a\": 1}\n{\"b\": 2}\n").unwrap();
        fs::rename(dir.join("g"), dir.join("f.jsonl")).unwrap();
        assert!(!same_file(&held, &dir.join("f.jsonl")).unwrap());
        drop(held);
        writer.join().unwrap().unwrap();
        assert_eq!(
            fs::read_to_string(dir.join("f.jsonl")).unwrap(),
            "{\"a\": 1}\n{\"b\": 2}\n{\"c\": 3}\n"
        );
        fs::remove_dir_all(&dir).unwrap();
    }
    #[test]
    fn a_newer_store_format_refuses_appends() {
        let dir = std::env::temp_dir().join(format!("kaiusap-format-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let end = || Instant::now() + std::time::Duration::from_secs(5);
        for malformed in ["", "not json", "{}", r#"{"format": "2"}"#, r#"{"format": 2.0}"#, r#"{"format": 1}"#] {
            fs::write(dir.join("store.json"), malformed).unwrap();
            append(&dir, "f.jsonl", r#"{"a": 1}"#, end()).unwrap();
        }
        fs::write(dir.join("store.json"), "{\"format\": 2, \"written_by\": \"0.9.0\"}\n").unwrap();
        let error = append(&dir, "g.jsonl", r#"{"a": 1}"#, end()).unwrap_err();
        assert_eq!(
            error.to_string(),
            "This store is format 2, written by kaiusap 0.9.0; this kaiusap only knows format 1. Install kaiusap 0.9.0 or later."
        );
        assert!(!dir.join("g.jsonl").exists());
        fs::write(dir.join("store.json"), r#"{"format": 3}"#).unwrap();
        let error = append(&dir, "g.jsonl", "{}", end()).unwrap_err();
        assert!(error.to_string().contains("Install a newer kaiusap."));
        assert_eq!(fs::read_to_string(dir.join("f.jsonl")).unwrap().lines().count(), 6);
        fs::remove_dir_all(&dir).unwrap();
    }
    #[test]
    fn fingerprints() {
        let dir = std::env::temp_dir().join(format!("kaiusap-native-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("f");
        fs::write(&path, b"abc").unwrap();
        let print = fingerprint(&path).unwrap();
        assert_eq!(print.split(':').nth(1), Some("3"));
        assert!(fingerprint(&dir.join("missing")).is_none());
        fs::remove_dir_all(&dir).unwrap();
    }
}
