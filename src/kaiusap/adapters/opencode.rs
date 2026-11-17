//! OpenCode's native part: the fast path of its constant hooks, `chat.message` and
//! `tool.execute.before`, behind the engine's `Provider` interface in `native/hook.rs`. The prompt record it
//! writes is byte-for-byte the one `opencode.py` writes for the same input.
use super::{get, log_error, quote, safe_id, string, Engine, Fields, Provider, Records, Screen};

pub struct Native;

impl Provider for Native {
    fn start(&self, event: &str) -> bool {
        event == "session.start"
    }
    fn screen(&self, event: &str, p: &Fields) -> Screen {
        if event == "tool.execute.before" {
            // The spawned-agent screen: a root caller's call is never refused, and a bash command without `kaiusap`
            // needs no decision; anything else is Python's, under the screen's 1 s.
            if !spawned(p) {
                return Screen::Done;
            }
            if get(p, "tool").as_deref() == Some("bash")
                && p.get("args")
                    .and_then(|v| super::fields(v))
                    .and_then(|v| get(&v, "command"))
                    .is_some_and(|s| !s.contains("kaiusap"))
            {
                return Screen::Done;
            }
            return Screen::Python;
        }
        // A child session's prompt: nothing to record or say.
        if event == "chat.message" && spawned(p) {
            return Screen::Done;
        }
        Screen::Continue
    }
    fn screen_timeout(&self, event: &str, p: &Fields) -> Option<String> {
        // The screen fails closed, as `OpenCodeAdapter.screen_timeout` does; a root caller is never refused.
        (event == "tool.execute.before" && spawned(p)).then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn input_refusal(&self, event: &str) -> Option<String> {
        // Input that didn't come whole within the screen's 1 s.
        (event == "tool.execute.before").then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn records(&self, event: &str, p: &Fields, at: &str, engine: &Engine) -> Records {
        let mut failed = false;
        let result = record(event, p, at, engine, &mut failed);
        if failed {
            return Records::Failed;
        }
        match result {
            None => Records::Python,
            Some(false) => Records::Skipped,
            Some(true) => Records::Appended,
        }
    }
    fn clean(&self, event: &str, p: &Fields, engine: &Engine) -> bool {
        event == "chat.message" && clean(p, engine)
    }
}

// A child session's input: an `agent_id` that is a non-empty string.
fn spawned(p: &Fields) -> bool {
    get(p, "agent_id").is_some_and(|s| !s.is_empty())
}

// `opencode.py`'s SCREEN_TIMEOUT_TEXT, and its deny as Python's json.dumps writes it.
const SCREEN_TIMEOUT_TEXT: &str = "kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they get through.";
fn deny(reason: &str) -> String {
    format!("{{\"hookSpecificOutput\": {{\"deny\": {}}}}}", quote(reason))
}

// The plugin's `chat.message` input, each key with its type; any other key is work.
const STRINGS: [&str; 5] = ["session_id", "agent_id", "cwd", "hook_event_name", "prompt"];
const NULLABLE: [&str; 3] = ["socket", "tmux_pane", "title"];
// A pid as Python writes it back: digits with no sign and no leading zero, so the raw text is the record's.
fn integer(v: &str) -> bool {
    !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()) && (v == "0" || !v.starts_with('0'))
}
fn known(p: &Fields) -> bool {
    // An empty `agent_id` is neither a root's input nor a child's: Python's to refuse.
    if get(p, "agent_id").is_some_and(|a| a.is_empty()) {
        return false;
    }
    p.iter().all(|(k, v)| match k.as_str() {
        k if STRINGS.contains(&k) => string(v).is_some(),
        k if NULLABLE.contains(&k) => v == "null" || string(v).is_some(),
        "pid" => integer(v),
        _ => false,
    })
}

// The OpenCode prompt record: opencode.py's keys in its order, `json.dumps`'s
// separators, `cwd` decoded from the input and quoted as it quotes, and the input's `pid` as written.
fn line(at: &str, sid: &str, cwd: &str, pid: &str) -> String {
    format!(
        "{{\"at\": {}, \"provider\": \"opencode\", \"session_id\": {}, \"session_key\": {}, \"source\": \"prompt\", \"cwd\": {}, \"pid\": {}}}",
        quote(at),
        quote(sid),
        quote(&format!("opencode:{sid}")),
        quote(cwd),
        pid
    )
}
fn record(event: &str, p: &Fields, at: &str, engine: &Engine, failed: &mut bool) -> Option<bool> {
    if event != "chat.message" {
        return Some(false);
    }
    if !known(p) {
        return None;
    }
    let sid = get(p, "session_id").filter(|s| !s.is_empty())?;
    if !safe_id(&sid) {
        return None;
    }
    if spawned(p) {
        return Some(true);
    }
    let cwd = get(p, "cwd")?;
    let pid = p.get("pid")?;
    // Anything possibly a notice stays Python's exact-text decision.
    if !engine.decided() && get(p, "prompt")?.contains("kaiusap:") {
        return Some(false);
    }
    if let Err(e) = engine.append("sessions/prompts.jsonl", &line(at, &sid, &cwd, pid)) {
        log_error(&format!("native append failed: {e}"));
        *failed = true;
    }
    Some(true)
}
fn clean(p: &Fields, engine: &Engine) -> bool {
    if !known(p) {
        return false;
    }
    let Some(sid) = get(p, "session_id").filter(|s| safe_id(s)) else {
        return false;
    };
    let Some(prompt) = get(p, "prompt") else {
        return false;
    };
    if get(p, "cwd").is_none() || prompt.contains("kaiusap:") || prompt.contains("Run kaiusap read ") {
        return false;
    }
    let Some(marker) = engine.acknowledged(&sid) else {
        return false;
    };
    // The title rule for the next prompt, with a default title counted as no name by Python, which
    // writes `equals` only with the handle as the title.
    match get(&marker, "title_mode").as_deref() {
        Some("any") => true,
        Some("equals") => get(&marker, "title").is_some() && get(&marker, "title") == get(p, "title"),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::super::fields;
    use super::*;
    fn input(text: &str) -> Fields {
        fields(text).unwrap()
    }
    #[test]
    fn record_line() {
        let at = "2026-11-10T15:00:00.000001Z";
        assert_eq!(
            line(at, "ses_a", "/tmp/café \t\"q\"", "4242"),
            format!(r#"{{"at": "{at}", "provider": "opencode", "session_id": "ses_a", "session_key": "opencode:ses_a", "source": "prompt", "cwd": "/tmp/café \t\"q\"", "pid": 4242}}"#)
        );
    }
    #[test]
    fn only_the_plugins_prompt_keys_are_known() {
        let base = r#""session_id":"ses_a","cwd":"/w","pid":7,"socket":null,"tmux_pane":null,"title":"t","hook_event_name":"chat.message","prompt":"hi""#;
        assert!(known(&input(&format!("{{{base}}}"))));
        assert!(known(&input(&format!(r#"{{{base},"agent_id":"ses_b"}}"#))));
        for extra in [r#","pid2":1"#, r#","created_here":true"#, r#","transcript_path":"/t""#] {
            assert!(!known(&input(&format!("{{{base}{extra}}}"))), "{extra}");
        }
        assert!(!known(&input(r#"{"pid":7.5}"#)));
        assert!(!known(&input(r#"{"pid":-0}"#)) && !known(&input(r#"{"pid":-7}"#)));
        assert!(!known(&input(r#"{"agent_id":""}"#)));
        assert!(!known(&input(r#"{"pid":"7"}"#)));
        assert!(!known(&input(r#"{"cwd":null}"#)));
    }
    #[test]
    fn screens() {
        let screen = |event: &str, text: &str| Native.screen(event, &input(text));
        assert!(matches!(screen("tool.execute.before", r#"{"tool":"kaiusap_send"}"#), Screen::Done));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"bash","args":{"command":"ls"}}"#), Screen::Done));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"bash","args":{"command":"kaiusap send"}}"#), Screen::Python));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"kaiusap_who"}"#), Screen::Python));
        assert!(matches!(screen("chat.message", r#"{"agent_id":"c"}"#), Screen::Done));
        assert!(matches!(screen("chat.message", r#"{"session_id":"s"}"#), Screen::Continue));
        let refusal = Native.screen_timeout("tool.execute.before", &input(r#"{"agent_id":"c"}"#)).unwrap();
        assert!(refusal.starts_with(r#"{"hookSpecificOutput": {"deny": "kaiusap couldn't check"#));
        assert!(Native.screen_timeout("tool.execute.before", &input(r#"{"tool":"bash"}"#)).is_none());
        assert_eq!(Native.input_refusal("tool.execute.before"), Some(refusal));
        assert!(Native.input_refusal("chat.message").is_none());
        assert!(Native.start("session.start") && !Native.start("chat.message"));
    }
}
