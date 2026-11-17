//! Claude's native part: the constant hooks' fast path, behind the engine's
//! `Provider` interface in `native/hook.rs`. The records it writes are byte-for-byte the ones
//! `claude.py` writes for the same input. The activity records (`activity`) are written here only:
//! `claude.py` reads them, and its hook path writes none (its `events` maps those events to nothing).
use super::{
    fields, fingerprint, get, log_error, quote, root, safe_id, string, Engine, Fields, Provider,
    Records, Screen,
};

pub struct Native;

impl Provider for Native {
    fn start(&self, event: &str) -> bool {
        event == "SessionStart"
    }
    fn screen(&self, event: &str, p: &Fields) -> Screen {
        // The spawned-agent screen: decided before any store work; a Bash command without `kaiusap` returns at once.
        if event == "PreToolUse" {
            if !spawned(p) {
                return Screen::Done;
            }
            if get(p, "tool_name").as_deref() == Some("Bash") {
                if p.get("tool_input")
                    .and_then(|v| fields(v))
                    .and_then(|v| get(&v, "command"))
                    .is_some_and(|s| !s.contains("kaiusap"))
                {
                    return Screen::Done;
                }
            }
            return Screen::Python;
        }
        // A spawned agent's prompt: nothing to record or say.
        if event == "UserPromptSubmit" && get(p, "agent_id").is_some_and(|s| !s.is_empty()) {
            return Screen::Done;
        }
        Screen::Continue
    }
    fn screen_timeout(&self, event: &str, p: &Fields) -> Option<String> {
        // The spawned-agent screen fails closed: a spawned agent's call Python couldn't check in time is refused, as
        // `ClaudeAdapter.screen_timeout` refuses it. A root caller is never refused.
        (event == "PreToolUse" && spawned(p)).then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn input_refusal(&self, event: &str) -> Option<String> {
        // PreToolUse input that didn't come whole within the screen's 1 s: the same refusal, since the call
        // couldn't be checked in time. Without input a root caller may be refused once.
        (event == "PreToolUse").then(|| deny(SCREEN_TIMEOUT_TEXT))
    }
    fn records(&self, event: &str, p: &Fields, at: &str, engine: &Engine) -> Records {
        if ACTIVITY.contains(&event) {
            return activity(event, p, at, engine);
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
        event == "UserPromptSubmit" && clean(p, engine)
    }
}

// A `PreToolUse` input with an `agent_id` that isn't null or empty: a spawned agent's call.
fn spawned(p: &Fields) -> bool {
    p.contains_key("agent_id")
        && p.get("agent_id").map(String::as_str) != Some("null")
        && !get(p, "agent_id").is_some_and(|s| s.is_empty())
}

// `activity`'s record-only hooks: one line each in the adapter's own store, and no output, so `Stop`
// never keeps Claude from stopping and `PermissionRequest` never decides.
const ACTIVITY: [&str; 4] = ["PermissionRequest", "Stop", "StopFailure", "Elicitation"];
const ACTIVITY_FILE: &str = "sessions/claude/activity.jsonl";
fn nullable(value: Option<&str>) -> String {
    value.map_or_else(|| "null".to_string(), quote)
}
fn activity_line(at: &str, kind: &str, sid: &str, agent: Option<&str>, tool: Option<&str>) -> String {
    format!(
        "{{\"at\": {}, \"kind\": {}, \"session_id\": {}, \"agent_id\": {}, \"tool\": {}}}",
        quote(at),
        quote(kind),
        quote(sid),
        nullable(agent),
        nullable(tool)
    )
}
fn activity(event: &str, p: &Fields, at: &str, engine: &Engine) -> Records {
    // An input without a safe session id, or with an agent id that isn't a string, owes no record.
    let Some(sid) = get(p, "session_id").filter(|s| safe_id(s)) else {
        return Records::Done;
    };
    let agent = match p.get("agent_id").map(String::as_str) {
        None | Some("null") => None,
        Some(raw) => match string(raw) {
            Some(agent) if agent.is_empty() => None,
            Some(agent) => Some(agent),
            None => return Records::Done,
        },
    };
    let tool = (event == "PermissionRequest").then(|| get(p, "tool_name")).flatten();
    let line = activity_line(at, event, &sid, agent.as_deref(), tool.as_deref());
    if let Err(e) = engine.append(ACTIVITY_FILE, &line) {
        log_error(&format!("native append failed: {e}"));
        return Records::Failed;
    }
    Records::Done
}

// `claude.py`'s SCREEN_TIMEOUT_TEXT, and its deny as Python's json.dumps writes it.
const SCREEN_TIMEOUT_TEXT: &str = "kaiusap couldn't check this call in time, so it is refused. You may retry it: a spawned agent may only look (whoami, who, inbox, read, help, pause), and those calls are allowed when they get through.";
fn deny(reason: &str) -> String {
    format!(
        "{{\"hookSpecificOutput\": {{\"hookEventName\": \"PreToolUse\", \"permissionDecision\": \"deny\", \"permissionDecisionReason\": {}}}}}",
        quote(reason)
    )
}

// The Claude prompt record (added 2026-11-06): no cwd, top-level sessions only.
// Its text is exactly what Python's json.dumps writes for the same record.
fn line(at: &str, sid: &str) -> String {
    format!(
        "{{\"at\": {}, \"provider\": \"claude\", \"session_id\": {}, \"session_key\": {}, \"source\": \"prompt\"}}",
        quote(at),
        quote(sid),
        quote(&format!("claude:{sid}"))
    )
}
fn record(
    event: &str,
    p: &Fields,
    at: &str,
    engine: &Engine,
    failed: &mut bool,
) -> Option<bool> {
    if event != "UserPromptSubmit" {
        return Some(false);
    }
    let sid = get(p, "session_id")?;
    if !safe_id(&sid) {
        return None;
    }
    match p.get("agent_id").map(String::as_str) {
        None | Some("null") => (),
        Some(raw) => match string(raw) {
            Some(agent) if agent.is_empty() => (),
            Some(_) => return Some(true),
            None => return None,
        },
    }
    // Anything possibly containing a notice, or an unexpected prompt, stays Python's decision.
    if !engine.decided() {
        match get(p, "prompt") {
            None => return None,
            Some(prompt) if prompt.contains("kaiusap:") => return Some(false),
            Some(_) => (),
        }
    }
    if let Err(e) = engine.append("sessions/prompts.jsonl", &line(at, &sid)) {
        log_error(&format!("native append failed: {e}"));
        *failed = true;
    }
    Some(true)
}
// Claude Code's UserPromptSubmit hook input, each key with the type Claude sends, for
// `clean`. From the installed 2.1.319 bundle: the hook input base (session_id, transcript_path, cwd,
// scratchpad_dir and prompt_id when set, permission_mode, agent_id and agent_type when set, effort as
// {"level": <string>} when set) plus hook_event_name, prompt, session_title (absent when the session
// has no title) and, in one mode, source ("poll_event"). Any other key, Codex's turn_id and model
// included, is work.
const STRINGS: [&str; 10] = [
    "session_id",
    "transcript_path",
    "cwd",
    "scratchpad_dir",
    "prompt_id",
    "permission_mode",
    "agent_id",
    "agent_type",
    "hook_event_name",
    "prompt",
];
fn known(p: &Fields) -> bool {
    p.iter().all(|(k, v)| match k.as_str() {
        k if STRINGS.contains(&k) => string(v).is_some(),
        "source" => string(v).is_some(),
        "session_title" => v == "null" || string(v).is_some(),
        "effort" => fields(v).is_some_and(|e| {
            e.keys().all(|k| k == "level") && e.get("level").is_some_and(|l| string(l).is_some())
        }),
        _ => false,
    })
}
fn clean(p: &Fields, engine: &Engine) -> bool {
    let Some(sid) = get(p, "session_id").filter(|s| safe_id(s)) else {
        return false;
    };
    let Some(prompt) = get(p, "prompt") else {
        return false;
    };
    if !known(p) {
        return false;
    }
    if get(p, "cwd").is_none() {
        return false;
    }
    if prompt.contains("kaiusap:") || prompt.contains("Run kaiusap read ") {
        return false;
    }
    let Some(marker) = engine.acknowledged(&sid) else {
        return false;
    };
    // The transcript index Python verified, and the title rules.
    let index = root().join("sessions/claude/last").join(format!("{sid}.json"));
    let Some(index) = fingerprint(&index) else {
        return false;
    };
    if get(&marker, "index_signature") != Some(index) {
        return false;
    }
    let transcript = get(p, "transcript_path").filter(|s| !s.is_empty());
    if get(&marker, "transcript") != transcript {
        return false;
    }
    match get(&marker, "title_mode").as_deref() {
        Some("any") => (),
        Some("equals") if get(&marker, "title") == get(p, "session_title") => (),
        _ => return false,
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    fn input(text: &str) -> Fields {
        fields(text).unwrap()
    }
    #[test]
    fn only_claude_prompt_keys_are_known() {
        let base = r#""session_id":"s","transcript_path":"/t.jsonl","cwd":"/w","permission_mode":"default","hook_event_name":"UserPromptSubmit","prompt":"hi""#;
        let with = |extra: &str| input(&format!("{{{base}{extra}}}"));
        assert!(known(&with("")));
        assert!(known(&with(
            r#","scratchpad_dir":"/s","prompt_id":"p","agent_type":"x","effort":{"level":"high"},"session_title":null,"source":"poll_event""#
        )));
        assert!(known(&with(r#","session_title":"alpha","agent_id":"""#)));
        // Codex's keys, and any other key, are work.
        for extra in [r#","turn_id":"t""#, r#","model":"m""#, r#","other":1"#] {
            assert!(!known(&with(extra)), "{extra}");
        }
        // Each key with the type Claude sends.
        for extra in [
            r#","effort":"high""#,
            r#","effort":{}"#,
            r#","effort":{"level":2}"#,
            r#","effort":{"level":"high","budget":1}"#,
            r#","prompt_id":null"#,
            r#","scratchpad_dir":["/x"]"#,
            r#","agent_id":null"#,
            r#","source":1"#,
            r#","session_title":3"#,
        ] {
            assert!(!known(&with(extra)), "{extra}");
        }
        assert!(!known(&input(r#"{"transcript_path":null}"#)));
    }
    #[test]
    fn prompt_line() {
        assert_eq!(
            line("2026-11-06T15:00:00.000001Z", "abc-1"),
            r#"{"at": "2026-11-06T15:00:00.000001Z", "provider": "claude", "session_id": "abc-1", "session_key": "claude:abc-1", "source": "prompt"}"#
        );
    }
    #[test]
    fn activity_lines() {
        assert_eq!(
            activity_line("2026-11-13T12:00:00.000001Z", "PermissionRequest", "s-1", Some("a\"1"), Some("Bash")),
            r#"{"at": "2026-11-13T12:00:00.000001Z", "kind": "PermissionRequest", "session_id": "s-1", "agent_id": "a\"1", "tool": "Bash"}"#
        );
        assert_eq!(
            activity_line("2026-11-13T12:00:00.000001Z", "Stop", "s-1", None, None),
            r#"{"at": "2026-11-13T12:00:00.000001Z", "kind": "Stop", "session_id": "s-1", "agent_id": null, "tool": null}"#
        );
    }
    #[test]
    fn activity_hooks_are_constant_hooks_with_no_screen() {
        for event in ACTIVITY {
            assert!(!Native.start(event));
            assert!(matches!(Native.screen(event, &input(r#"{"agent_id":"a"}"#)), Screen::Continue));
            assert!(Native.screen_timeout(event, &input(r#"{"agent_id":"a"}"#)).is_none());
            assert!(Native.input_refusal(event).is_none());
        }
    }
    #[test]
    fn start_hook() {
        assert!(Native.start("SessionStart"));
        assert!(!Native.start("UserPromptSubmit"));
        assert!(!Native.start("PreToolUse"));
    }
    #[test]
    fn pre_tool_screen() {
        let screen = |text: &str| Native.screen("PreToolUse", &input(text));
        assert!(matches!(screen(r#"{"tool_name":"Bash"}"#), Screen::Done));
        assert!(matches!(screen(r#"{"agent_id":null}"#), Screen::Done));
        assert!(matches!(screen(r#"{"agent_id":""}"#), Screen::Done));
        let bash = |command: &str| {
            screen(&format!(
                r#"{{"agent_id":"a","tool_name":"Bash","tool_input":{{"command":{}}}}}"#,
                quote(command)
            ))
        };
        assert!(matches!(bash("ls"), Screen::Done));
        assert!(matches!(bash("kaiusap send"), Screen::Python));
        assert!(matches!(
            screen(r#"{"agent_id":"a","tool_name":"mcp__kaiusap__send"}"#),
            Screen::Python
        ));
        assert!(matches!(screen(r#"{"agent_id":"a","tool_name":"Bash"}"#), Screen::Python));
    }
    #[test]
    fn screen_timeout_refuses_only_a_spawned_call() {
        let timeout = |event: &str, text: &str| Native.screen_timeout(event, &input(text));
        let refusal = timeout("PreToolUse", r#"{"agent_id":"a","tool_name":"mcp__kaiusap__send"}"#).unwrap();
        assert!(refusal.starts_with(r#"{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "kaiusap couldn't check"#));
        assert!(refusal.ends_with(r#"get through."}}"#));
        for root in [r#"{"tool_name":"Bash"}"#, r#"{"agent_id":null}"#, r#"{"agent_id":""}"#] {
            assert!(timeout("PreToolUse", root).is_none(), "{root}");
        }
        assert!(timeout("UserPromptSubmit", r#"{"agent_id":"a"}"#).is_none());
        assert!(timeout("SessionStart", r#"{"agent_id":"a"}"#).is_none());
    }
    #[test]
    fn input_refusal_is_the_screen_refusal_for_the_screen_only() {
        let spawned = r#"{"agent_id":"a","tool_name":"mcp__kaiusap__send"}"#;
        assert_eq!(Native.input_refusal("PreToolUse"), Native.screen_timeout("PreToolUse", &input(spawned)));
        assert!(Native.input_refusal("UserPromptSubmit").is_none());
        assert!(Native.input_refusal("SessionStart").is_none());
    }
    #[test]
    fn prompt_screen() {
        let screen = |text: &str| Native.screen("UserPromptSubmit", &input(text));
        assert!(matches!(screen(r#"{"agent_id":"a"}"#), Screen::Done));
        assert!(matches!(screen(r#"{"agent_id":""}"#), Screen::Continue));
        assert!(matches!(screen(r#"{"session_id":"s"}"#), Screen::Continue));
        assert!(matches!(
            Native.screen("SessionStart", &input(r#"{"agent_id":"a"}"#)),
            Screen::Continue
        ));
    }
}
