//! OpenCode 2's native part: the fast path of its constant hooks, `session.prompt` and
//! `tool.execute.before`, behind the engine's `Provider` interface in `native/hook.rs`. The prompt record it
//! writes is byte-for-byte the one `opencode2.py` writes for the same input. `tui.route`, the terminal part's
//! report, has the start hook's 3 s and is Python's.
use super::{get, log_error, quote, safe_id, string, Engine, Fields, Provider, Records, Screen};

pub struct Native;

impl Provider for Native {
    fn start(&self, event: &str) -> bool {
        event == "session.start" || event == "tui.route"
    }
    fn screen(&self, event: &str, p: &Fields) -> Screen {
        if event == "tool.execute.before" {
            // The spawned-agent screen: a root caller's call is never refused, and a shell command without `kaiusap`
            // needs no decision; anything else is Python's, under the screen's 1 s.
            if !spawned(p) {
                return Screen::Done;
            }
            if get(p, "tool").as_deref() == Some("shell")
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
        if event == "session.prompt" && spawned(p) {
            return Screen::Done;
        }
        Screen::Continue
    }
    fn screen_timeout(&self, event: &str, p: &Fields) -> Option<String> {
        // The screen fails closed, as `OpenCode2Adapter.screen_timeout` does; a root caller is never refused.
        (event == "tool.execute.before" && spawned(p)).then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn input_refusal(&self, event: &str) -> Option<String> {
        // Input that didn't come whole within the screen's 1 s.
        (event == "tool.execute.before").then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn records(&self, event: &str, p: &Fields, at: &str, engine: &Engine) -> Records {
        // kaiusap's own notice, known by its `metadata` tag: no record and no work, so Python doesn't start; the
        // delivery recorded its mention.
        if event == "session.prompt" && !engine.decided() && known(p) && notice(p) {
            return Records::Done;
        }
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
        event == "session.prompt" && clean(p, engine)
    }
}

// A child session's input: an `agent_id` that is a non-empty string.
fn spawned(p: &Fields) -> bool {
    get(p, "agent_id").is_some_and(|s| !s.is_empty())
}

// `opencode2.py`'s SCREEN_TIMEOUT_TEXT, and its deny as Python's json.dumps writes it.
const SCREEN_TIMEOUT_TEXT: &str = "kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they get through.";
fn deny(reason: &str) -> String {
    format!("{{\"hookSpecificOutput\": {{\"deny\": {}}}}}", quote(reason))
}

// The plugin's `session.prompt` input, each key with its type; any other key is work. `metadata` is null or an
// object; its `kaiusap` key marks kaiusap's own notice, which writes no prompt record.
const STRINGS: [&str; 5] = ["session_id", "agent_id", "cwd", "hook_event_name", "prompt"];
fn known(p: &Fields) -> bool {
    // An empty `agent_id` is neither a root's input nor a child's: Python's to refuse.
    if get(p, "agent_id").is_some_and(|a| a.is_empty()) {
        return false;
    }
    p.iter().all(|(k, v)| match k.as_str() {
        k if STRINGS.contains(&k) => string(v).is_some(),
        "title" => v == "null" || string(v).is_some(),
        "metadata" => v == "null" || super::fields(v).is_some(),
        _ => false,
    })
}

// The OpenCode 2 prompt record: opencode2.py's keys in its order, `json.dumps`'s
// separators, `cwd` decoded from the input and quoted as it quotes. No pid: the service runs the session.
fn line(at: &str, sid: &str, cwd: &str) -> String {
    format!(
        "{{\"at\": {}, \"provider\": \"opencode2\", \"session_id\": {}, \"session_key\": {}, \"source\": \"prompt\", \"cwd\": {}}}",
        quote(at),
        quote(sid),
        quote(&format!("opencode2:{sid}")),
        quote(cwd)
    )
}
fn record(event: &str, p: &Fields, at: &str, engine: &Engine, failed: &mut bool) -> Option<bool> {
    if event != "session.prompt" {
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
    if notice(p) {
        return Some(false);
    }
    if let Err(e) = engine.append("sessions/prompts.jsonl", &line(at, &sid, &cwd)) {
        log_error(&format!("native append failed: {e}"));
        *failed = true;
    }
    Some(true)
}
fn notice(p: &Fields) -> bool {
    p.get("metadata")
        .and_then(|v| super::fields(v))
        .is_some_and(|m| m.contains_key("kaiusap"))
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
    if get(p, "cwd").is_none() || notice(p) || prompt.contains("kaiusap:") || prompt.contains("Run kaiusap read ") {
        return false;
    }
    engine.acknowledged(&sid).is_some()
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
        let at = "2026-11-12T15:00:00.000001Z";
        assert_eq!(
            line(at, "ses_a", "/tmp/café \t\"q\""),
            format!(r#"{{"at": "{at}", "provider": "opencode2", "session_id": "ses_a", "session_key": "opencode2:ses_a", "source": "prompt", "cwd": "/tmp/café \t\"q\""}}"#)
        );
    }
    #[test]
    fn only_the_plugins_prompt_keys_are_known() {
        let base = r#""session_id":"ses_a","cwd":"/w","title":null,"hook_event_name":"session.prompt","prompt":"hi","metadata":null"#;
        assert!(known(&input(&format!("{{{base}}}"))));
        assert!(known(&input(&format!(r#"{{{base},"agent_id":"ses_b"}}"#))));
        for extra in [r#","pid":1"#, r#","socket":null"#, r#","shows":"ses_b""#] {
            assert!(!known(&input(&format!("{{{base}{extra}}}"))), "{extra}");
        }
        assert!(known(&input(r#"{"metadata":{"kaiusap":"x"}}"#)) && notice(&input(r#"{"metadata":{"kaiusap":"x"}}"#)));
        assert!(!notice(&input(r#"{"metadata":{"source":"x"}}"#)) && !notice(&input(r#"{"metadata":null}"#)));
        assert!(!known(&input(r#"{"metadata":"x"}"#)));
        assert!(!known(&input(r#"{"agent_id":""}"#)));
        assert!(!known(&input(r#"{"cwd":null}"#)));
    }
    #[test]
    fn screens() {
        let screen = |event: &str, text: &str| Native.screen(event, &input(text));
        assert!(matches!(screen("tool.execute.before", r#"{"tool":"kaiusap_send"}"#), Screen::Done));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"shell","args":{"command":"ls"}}"#), Screen::Done));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"shell","args":{"command":"kaiusap send"}}"#), Screen::Python));
        assert!(matches!(screen("tool.execute.before", r#"{"agent_id":"c","tool":"kaiusap_who"}"#), Screen::Python));
        assert!(matches!(screen("session.prompt", r#"{"agent_id":"c"}"#), Screen::Done));
        assert!(matches!(screen("session.prompt", r#"{"session_id":"s"}"#), Screen::Continue));
        let refusal = Native.screen_timeout("tool.execute.before", &input(r#"{"agent_id":"c"}"#)).unwrap();
        assert!(refusal.starts_with(r#"{"hookSpecificOutput": {"deny": "kaiusap couldn't check"#));
        assert!(Native.screen_timeout("tool.execute.before", &input(r#"{"tool":"shell"}"#)).is_none());
        assert_eq!(Native.input_refusal("tool.execute.before"), Some(refusal));
        assert!(Native.input_refusal("session.prompt").is_none());
        assert!(Native.start("session.start") && Native.start("tui.route") && !Native.start("session.prompt"));
    }
    #[test]
    fn a_notice_is_no_work() {
        let tagged = input(r#"{"session_id":"ses_a","cwd":"/w","title":null,"prompt":"kaiusap: message from b","metadata":{"kaiusap":"x"}}"#);
        assert!(known(&tagged) && notice(&tagged));
        let plain = input(r#"{"session_id":"ses_a","cwd":"/w","title":null,"prompt":"kaiusap: message from b","metadata":null}"#);
        assert!(known(&plain) && !notice(&plain));
    }
}
