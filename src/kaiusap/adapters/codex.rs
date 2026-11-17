//! Codex's native part: the constant hooks' fast path, behind the engine's
//! `Provider` interface in `native/hook.rs`. The records it writes are byte-for-byte the ones
//! `codex.py` writes for the same input.
use super::{get, log_error, quote, safe_id, string, uuid, Engine, Fields, Provider, Records};
use std::process::Command;

pub struct Native;

impl Provider for Native {
    fn start(&self, event: &str) -> bool {
        event == "SessionStart"
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
            // A subagent start is record-only; an unidentified or spawned caller gets nothing.
            Some(true) if event == "SubagentStart"
                || p.get("transcript_path").map(String::as_str) == Some("null")
                || spawned(p) =>
            {
                Records::Done
            }
            Some(true) => Records::Appended,
        }
    }
    fn clean(&self, event: &str, p: &Fields, engine: &Engine) -> bool {
        event == "UserPromptSubmit" && clean(p, engine)
    }
}

fn spawned(p: &Fields) -> bool {
    get(p, "agent_id").is_some_and(|a| !a.is_empty())
        || get(p, "transcript_path")
            .and_then(|v| transcript_id(&v).map(str::to_string))
            .is_some_and(|id| Some(id) != get(p, "session_id"))
}
fn transcript_id(path: &str) -> Option<&str> {
    let name = path.rsplit('/').next()?;
    let i = name.find("rollout-")?;
    let stamp = name.get(i + 8..i + 27)?;
    if !stamp.bytes().enumerate().all(|(n, c)| match n {
        4 | 7 | 13 | 16 => c == b'-',
        10 => c == b'T',
        _ => c.is_ascii_digit(),
    }) {
        return None;
    }
    if name.as_bytes().get(i + 27) != Some(&b'-') {
        return None;
    }
    let rest = name.get(i + 28..)?;
    let id = rest.get(..36)?;
    if uuid(id) && (rest.get(36..)? == ".jsonl" || rest.get(36..)?.starts_with('_')) {
        Some(id)
    } else {
        None
    }
}
fn title_shows(title: &str, thread: &str) -> bool {
    for (i, _) in title.char_indices() {
        let rest = &title[i..];
        let n = if rest.starts_with(thread) {
            36
        } else if rest.starts_with(&format!("{}...", &thread[..29])) {
            32
        } else {
            continue;
        };
        let word = |c: char| c.is_ascii_hexdigit() && !c.is_ascii_uppercase() || c == '-';
        if title[..i].chars().next_back().is_some_and(word)
            || rest[n..].chars().next().is_some_and(word)
        {
            continue;
        }
        return true;
    }
    false
}
// The prompt and subagent records, in the store's line format: codex.py's
// keys in its order, `json.dumps`'s separators, and `cwd` decoded from the input and quoted as it quotes.
fn line(at: &str, sid: &str, key: &str, source: &str, cwd: &str, agent: Option<&String>) -> String {
    format!(
        "{{\"at\": {}, \"provider\": \"codex\", \"session_id\": {}, \"session_key\": {}, \"source\": {}{}, \"cwd\": {}}}",
        quote(at),
        quote(sid),
        quote(key),
        quote(source),
        agent.map(|a| format!(", \"agent_id\": {}", quote(a))).unwrap_or_default(),
        quote(cwd)
    )
}
// The terminal map, in the adapter's own store directory.
const TERMINAL_MAP: &str = "sessions/codex/terminals.jsonl";
// A terminal-map `prompt` line for a pane whose title shows the thread, in the same format.
fn terminal_line(at: &str, cols: &[&str], sid: &str, key: &str) -> String {
    format!(
        "{{\"at\": {}, \"pane\": {}, \"codex_pid\": null, \"codex_started_at\": null, \"thread_prefix\": {}, \"thread_id\": {}, \"source\": \"prompt\", \"session_key\": {}}}",
        quote(at),
        quote(&format!("{}@{}:{}", cols[5], cols[6], cols[0])),
        quote(&sid[..29]),
        quote(sid),
        quote(key)
    )
}
fn record(
    event: &str,
    p: &Fields,
    at: &str,
    engine: &Engine,
    failed: &mut bool,
) -> Option<bool> {
    if !["UserPromptSubmit", "SubagentStart"].contains(&event) {
        return Some(false);
    }
    let sid = get(p, "session_id")?;
    if p.get("agent_id")
        .is_some_and(|v| v != "null" && string(v).is_none())
    {
        return None;
    }
    let transcript = match p.get("transcript_path")?.as_str() {
        "null" => return Some(true),
        raw => string(raw)?,
    };
    let agent = get(p, "agent_id").filter(|v| !v.is_empty()).or_else(|| {
        transcript_id(&transcript)
            .filter(|id| *id != sid)
            .map(str::to_string)
    });
    if event == "UserPromptSubmit" && agent.is_some() {
        return Some(true);
    }
    if event == "SubagentStart" && agent.is_none() {
        return Some(true);
    }
    if !safe_id(&sid) || (event == "UserPromptSubmit" && !uuid(&sid)) {
        return None;
    }
    let cwd = get(p, "cwd")?;
    if event == "UserPromptSubmit" {
        let prompt = get(p, "prompt")?;
        // Anything possibly containing a notice stays Python's exact-text decision.
        if !engine.decided() && prompt.contains("kaiusap:") {
            return Some(false);
        }
    }
    let key = format!(
        "codex:{sid}{}",
        agent.as_ref().map(|a| format!("/{a}")).unwrap_or_default()
    );
    let source = if event == "SubagentStart" { "subagent" } else { "prompt" };
    if let Err(e) = engine.append(
        if event == "SubagentStart" {
            "sessions/starts.jsonl"
        } else {
            "sessions/prompts.jsonl"
        },
        &line(at, &sid, &key, source, &cwd, agent.as_ref()),
    ) {
        log_error(&format!("native append failed: {e}"));
        *failed = true;
        return Some(true);
    }
    if event == "SubagentStart" {
        return Some(true);
    }
    if !uuid(&sid) {
        return Some(true);
    }
    // The pane scan: one list-panes call, no `ps`.
    let mut command = Command::new("tmux");
    command.args(["-L","default","list-panes","-a","-F","#{pane_id}\t#{session_name}\t#{session_attached}\t#{pane_pid}\t#{pane_tty}\t#{pid}\t#{start_time}\t#{pane_title}"]);
    if let Some((true, bytes)) = engine.run(command, None) {
        let Ok(output) = std::str::from_utf8(&bytes) else {
            log_error("unrecognised native tmux output");
            return Some(true);
        };
        for row in output.lines() {
            let cols: Vec<_> = row.splitn(8, '\t').collect();
            if cols.len() != 8 || !cols[3].bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            if title_shows(cols[7], &sid) {
                if let Err(e) = engine.append(TERMINAL_MAP, &terminal_line(at, &cols, &sid, &key)) {
                    log_error(&format!("native terminal append failed: {e}"));
                    *failed = true;
                    return Some(true);
                }
            }
        }
    } else {
        log_error("native tmux unavailable or deadline reached");
    }
    Some(true)
}
// Codex's UserPromptSubmit hook input, each key with the type Codex sends:
// `UserPromptSubmitCommandInput` in codex-rs/hooks/src/schema.rs, built in
// codex-rs/hooks/src/events/user_prompt_submit.rs. `agent_id` and `agent_type` are present only for a
// subagent; `transcript_path` is always present, a string or null. Any other key is work.
const STRINGS: [&str; 9] = [
    "session_id",
    "turn_id",
    "agent_id",
    "agent_type",
    "cwd",
    "hook_event_name",
    "model",
    "permission_mode",
    "prompt",
];
const NULLABLE: [&str; 1] = ["transcript_path"];
fn known(p: &Fields) -> bool {
    p.iter().all(|(k, v)| {
        if STRINGS.contains(&k.as_str()) {
            string(v).is_some()
        } else {
            NULLABLE.contains(&k.as_str()) && (v == "null" || string(v).is_some())
        }
    })
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
    if get(p, "cwd").is_none() {
        return false;
    }
    if prompt.contains("kaiusap:") || prompt.contains("Run kaiusap read ") {
        return false;
    }
    engine.acknowledged(&sid).is_some()
}

#[cfg(test)]
mod tests {
    use super::super::fields;
    use super::*;
    const ID: &str = "12345678-1234-1234-1234-123456789abc";
    #[test]
    fn rollout_and_titles() {
        let id = ID;
        assert_eq!(
            transcript_id(&format!("/tmp/rollout-2026-11-04T12-00-00-{id}.jsonl")),
            Some(id)
        );
        assert_eq!(
            transcript_id(&format!("/tmp/rollout-2026-11-04T12-00-00-{id}_x.jsonl")),
            Some(id)
        );
        assert!(title_shows(&format!("Codex {id}"), id));
        assert!(title_shows(&format!("Codex {}...", &id[..29]), id));
        assert!(!title_shows(&format!("a{id}"), id));
        assert!(!title_shows(&format!("{id}0"), id));
    }
    #[test]
    fn record_lines() {
        let at = "2026-11-06T15:00:00.000001Z";
        let key = format!("codex:{ID}");
        // Each expected line is Python's json.dumps(record, ensure_ascii=False) for codex.py's record.
        assert_eq!(
            line(at, ID, &key, "prompt", "/tmp/café \t\"q\"", None),
            format!(r#"{{"at": "{at}", "provider": "codex", "session_id": "{ID}", "session_key": "{key}", "source": "prompt", "cwd": "/tmp/café \t\"q\""}}"#)
        );
        let agent = "a1".to_string();
        assert_eq!(
            line(at, ID, &format!("{key}/a1"), "subagent", "/tmp/x", Some(&agent)),
            format!(r#"{{"at": "{at}", "provider": "codex", "session_id": "{ID}", "session_key": "{key}/a1", "source": "subagent", "agent_id": "a1", "cwd": "/tmp/x"}}"#)
        );
        let cols = ["%5", "s", "1", "77", "/dev/ttys001", "4321", "1793606400", ID];
        assert_eq!(
            terminal_line(at, &cols, ID, &key),
            format!(r#"{{"at": "{at}", "pane": "4321@1793606400:%5", "codex_pid": null, "codex_started_at": null, "thread_prefix": "{}", "thread_id": "{ID}", "source": "prompt", "session_key": "{key}"}}"#, &ID[..29])
        );
    }
    #[test]
    fn only_codex_prompt_keys_are_known() {
        let input = |extra: &str| {
            fields(&format!(
                r#"{{"session_id":"{ID}","turn_id":"t","transcript_path":null,"cwd":"/w","hook_event_name":"UserPromptSubmit","model":"m","permission_mode":"default","prompt":"hi"{extra}}}"#
            ))
            .unwrap()
        };
        assert!(known(&input("")));
        assert!(known(&input(r#","agent_id":"a","agent_type":"worker""#)));
        // Claude's keys, and any other key, are work.
        for extra in [
            r#","prompt_id":"p""#,
            r#","scratchpad_dir":"/s""#,
            r#","effort":{"level":"high"}"#,
            r#","session_title":"t""#,
            r#","source":"poll_event""#,
            r#","other":1"#,
        ] {
            assert!(!known(&input(extra)), "{extra}");
        }
        // Each key with the type Codex sends.
        for extra in [r#","turn_id":7"#, r#","model":null"#, r#","agent_id":null"#, r#","agent_type":1"#] {
            let mut p = input("");
            let (k, v) = extra[1..].split_once(':').unwrap();
            p.insert(k.trim_matches('"').to_string(), v.to_string());
            assert!(!known(&p), "{extra}");
        }
        let mut p = input("");
        p.insert("transcript_path".into(), "3".into());
        assert!(!known(&p));
    }
    #[test]
    fn start_hook_and_spawned_callers() {
        assert!(Native.start("SessionStart"));
        assert!(!Native.start("UserPromptSubmit"));
        assert!(!Native.start("SubagentStart"));
        let input = |text: String| fields(&text).unwrap();
        assert!(spawned(&input(format!(r#"{{"session_id":"{ID}","agent_id":"a"}}"#))));
        assert!(!spawned(&input(format!(r#"{{"session_id":"{ID}","agent_id":""}}"#))));
        let review = "87654321-1234-1234-1234-123456789abc";
        assert!(spawned(&input(format!(
            r#"{{"session_id":"{ID}","transcript_path":"/t/rollout-2026-11-04T12-00-00-{review}.jsonl"}}"#
        ))));
        assert!(!spawned(&input(format!(
            r#"{{"session_id":"{ID}","transcript_path":"/t/rollout-2026-11-04T12-00-00-{ID}.jsonl"}}"#
        ))));
    }
}
