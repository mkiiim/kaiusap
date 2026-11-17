"""OpenCode 1.x's plugin socket: a reply arrives whole, however long (reported 2026-11-12: a context
reply cut at 8192 bytes). Runs the real plugin module under the Bun inside an installed OpenCode executable
(`BUN_BE_BUN=1`), with a fake OpenCode client; skipped when no OpenCode executable is found."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins/opencode/kaiusap.ts"

HARNESS = r'''
import { KaiusapPlugin } from "PLUGIN"
// A reply that never ends is a failure, not a hang.
setTimeout(() => { console.log(JSON.stringify({ timed_out: true })); process.exit(0) }, 10_000)
const SESSION = "ses_socket_test"
const title = "t".repeat(200_000)  // a reply far past one socket write
const models: Record<string, any> = {}
for (let i = 0; i < 2000; i++) models[`model-${i}-with-a-long-name`] = { limit: { context: 1000 + i } }
models["gemini-3.8-flash"] = { limit: { context: 1048576 } }
const client = {
  session: {
    get: async () => ({ data: { id: SESSION, parentID: null, title } }),
    messages: async () => ({ data: [{ info: { role: "assistant", modelID: "gemini-3.8-flash", providerID: "google",
      tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } }, time: { created: 1, completed: 2 } } }] }),
  },
  config: { providers: async () => ({ data: { providers: [{ id: "google", models }, { id: "other", models }] } }) },
  // The SDK's own HTTP client, through which the plugin asks its process for `/global/health`.
  _client: { get: async ({ url }: any) => (url === "/global/health" ? { data: { healthy: true, version: "1.19.5" } } : {}) },
}
const hooks: any = await KaiusapPlugin({ client, directory: process.cwd() })
// The plugin answers only for a session this process created or started: OpenCode's event for it.
await hooks.event({ event: { type: "session.created", properties: { info: { id: SESSION, parentID: null, title } } } })
const dir = process.env.XDG_RUNTIME_DIR + "/kaiusap-oc"
const { readdirSync } = await import("node:fs")
const socket = dir + "/" + readdirSync(dir).find((f: string) => f.endsWith(".sock"))
async function ask(request: any): Promise<string> {
  const chunks: Uint8Array[] = []
  await new Promise<void>((resolve, reject) => {
    Bun.connect({ unix: socket, socket: {
      open(s) { s.write(JSON.stringify(request) + "\n") },
      data(s, chunk) { chunks.push(new Uint8Array(chunk)) },
      close() { resolve() },
      error(s, e) { reject(e) },
    } })
  })
  return Buffer.concat(chunks).toString()
}
const session = await ask({ type: "session", session_id: SESSION })
const context = await ask({ type: "context", session_id: SESSION })
console.log(JSON.stringify({ session_length: session.length, session: JSON.parse(session).title === title,
                             context: JSON.parse(context) }))
process.exit(0)
'''


def bun():
    """An OpenCode executable to run as Bun: the one on the PATH, else the opencode2 install's."""
    for candidate in (shutil.which("opencode"), str(Path.home() / ".opencode2/app/node_modules/@opencode/cli/bin/opencode.exe")):
        if candidate and Path(candidate).exists():
            return os.path.realpath(candidate)
    return None


# An exception to conftest's rule that no test runs a real provider: OpenCode's executable is used only as the Bun
# it contains (`BUN_BE_BUN=1`), to run kaiusap's own plugin module, with HOME, KAIUSAP_HOME and the runtime
# directory in the test's temporary folder; no OpenCode command runs and no OpenCode state is read. Found, and
# the unpatched Popen kept, at import, before conftest isolates PATH and HOME.
BUN = bun()
POPEN = subprocess.Popen


@pytest.mark.skipif(BUN is None, reason="no OpenCode executable to run the plugin under")
def test_a_long_reply_arrives_whole_and_context_names_only_the_models_used(tmp_path):
    script = tmp_path / "harness.ts"
    script.write_text(HARNESS.replace("PLUGIN", str(PLUGIN)))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    env = {"BUN_BE_BUN": "1", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime),
           "KAIUSAP_HOME": str(tmp_path / "home"), "OPENCODE_PID": str(os.getpid())}
    with POPEN([BUN, "run", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
               cwd=tmp_path) as process:
        try:
            out, err = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
    assert process.returncode == 0, err
    found = json.loads(out.strip().splitlines()[-1])
    assert not found.get("timed_out"), "a socket reply never ended"
    assert found["session"] is True and found["session_length"] > 200_000  # the whole reply, past any one write
    assert found["context"]["ok"] is True
    assert found["context"]["limits"] == {"google/gemini-3.8-flash": 1048576}  # only the model the messages used
    assert found["context"]["version"] == "1.19.5"  # the running process's version, for harness_version
