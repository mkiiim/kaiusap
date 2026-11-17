// The one hook runner both parts of kaiusap's OpenCode 2 plugin share: `kaiusap hook opencode2
// <event>` with the hook input on stdin, and its output's `hookSpecificOutput`. It holds no rule of kaiusap's.
import { accessSync, appendFileSync, constants, mkdirSync } from "node:fs"

// The program the hooks run, found as the other providers' plugin launcher finds it,
// once per process: KAIUSAP_BIN, then ~/.local/bin/kaiusap, then `kaiusap` on the PATH.
function executable(path: string): boolean {
  try { accessSync(path, constants.X_OK); return true } catch { return false }
}
const LOCAL_BIN = `${process.env.HOME}/.local/bin/kaiusap`
const KAIUSAP = process.env.KAIUSAP_BIN && executable(process.env.KAIUSAP_BIN) ? process.env.KAIUSAP_BIN
  : executable(LOCAL_BIN) ? LOCAL_BIN : "kaiusap"
// A `kaiusap ...` command line with that program in place of its first word.
function resolved(argv: string[]): string[] {
  return [KAIUSAP, ...argv.slice(1)]
}

// How long a part waits for a hook process: the start hook's 3 s (`session.start`, `tui.route`) and the 1 s hard
// limit of the prompt hook (`session.prompt`) and the screen, each with a margin for starting the process.
const LIMIT_MS: Record<string, number> = { "session.start": 3500, "tui.route": 3500, "session.prompt": 1500, "tool.execute.before": 1500 }

export type Output = { tell?: string; deny?: string }

// The plugin's own failures go where the hooks' do, since OpenCode shows a plugin's errors nowhere.
export function logError(reason: string) {
  try {
    const home = process.env.KAIUSAP_HOME ?? `${process.env.HOME}/.kaiusap`
    mkdirSync(home, { recursive: true, mode: 0o700 })
    appendFileSync(`${home}/hook-errors.log`, `${new Date().toISOString().slice(0, 19)}Z opencode2 plugin: ${reason}\n`)
  } catch {}
}

export async function runHook(event: string, input: Record<string, unknown>): Promise<Output> {
  try {
    const proc = Bun.spawn(resolved(["kaiusap", "hook", "opencode2", event]), {
      stdin: "pipe", stdout: "pipe", stderr: "ignore", env: process.env,
    })
    proc.stdin.write(JSON.stringify(input))
    proc.stdin.end()
    const timer = setTimeout(() => proc.kill(), LIMIT_MS[event] ?? 1500)
    const text = await new Response(proc.stdout).text()
    await proc.exited
    clearTimeout(timer)
    const line = text.trim().split("\n").pop()
    if (!line) return {}
    const output = JSON.parse(line)
    const specific = output && typeof output === "object" ? output.hookSpecificOutput : null
    return specific && typeof specific === "object" ? specific : {}
  } catch (error) {
    logError(`${event}: ${error}`)
    return {}
  }
}

// A terminal's quit report, sent while the process exits: the one hook that can't wait for an answer.
export function runHookNow(event: string, input: Record<string, unknown>) {
  try {
    Bun.spawnSync(resolved(["kaiusap", "hook", "opencode2", event]), {
      stdin: new TextEncoder().encode(JSON.stringify(input)), stdout: "ignore", stderr: "ignore", env: process.env,
      timeout: LIMIT_MS[event] ?? 1500,
    })
  } catch (error) {
    logError(`${event}: ${error}`)
  }
}
