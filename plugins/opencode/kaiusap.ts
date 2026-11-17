// kaiusap's OpenCode plugin.
//
// It holds no rule of kaiusap's. For each event in HOOK_EVENTS it runs `kaiusap hook opencode <event>` with
// the hook input on stdin and carries out the output in OpenCode's terms: `tell` becomes a text part the
// model reads, `title` a session.update, `deny` an error thrown from tool.execute.before. Besides the hooks
// it reports facts only this process can see, serves the delivery socket, and writes the calling session's
// id into every kaiusap MCP call as `_kaiusap_session`. It adds no tools.
import { createHash } from "node:crypto"
import { accessSync, appendFileSync, constants, lstatSync, mkdirSync, unlinkSync } from "node:fs"

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

const HOOK_EVENTS = ["session.start", "chat.message", "tool.execute.before"]
const SCREENED_TOOLS = ["bash", "kaiusap_whoami", "kaiusap_who", "kaiusap_iam", "kaiusap_release", "kaiusap_send", "kaiusap_reply", "kaiusap_inbox", "kaiusap_read", "kaiusap_moot", "kaiusap_pause", "kaiusap_unpause", "kaiusap_help"]
const TOOL_PREFIX = "kaiusap_"
const IDENTITY_ARGUMENT = "_kaiusap_session"
// How long the plugin waits for a hook process: the start hook's 3 s and the 1 s hard limit of the
// constant hooks and the screen, each with a margin for starting the process.
const LIMIT_MS: Record<string, number> = { "session.start": 3500, "chat.message": 1500, "tool.execute.before": 1500 }
const MAX_LINE = 1 << 20

type Info = { parentID?: string | null; title?: string | null }
type Output = { tell?: string; title?: string; deny?: string }

// The plugin's own failures go where the hooks' do, since OpenCode shows a plugin's errors nowhere.
function logError(reason: string) {
  try {
    const home = process.env.KAIUSAP_HOME ?? `${process.env.HOME}/.kaiusap`
    mkdirSync(home, { recursive: true, mode: 0o700 })
    appendFileSync(`${home}/hook-errors.log`, `${new Date().toISOString().slice(0, 19)}Z opencode plugin: ${reason}\n`)
  } catch {}
}

function socketPath(directory: string): string | null {
  try {
    const runtime = process.env.XDG_RUNTIME_DIR
    const dir = runtime ? `${runtime}/kaiusap-oc` : `/tmp/kaiusap-oc-${process.getuid?.() ?? 0}`
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    // A directory someone else made, or a link, isn't ours to serve from.
    const found = lstatSync(dir)
    if (!found.isDirectory() || found.uid !== process.getuid?.() || (found.mode & 0o077) !== 0) {
      logError(`${dir} isn't a private directory of this user; no socket, so mail to this process is stored`)
      return null
    }
    const instance = createHash("sha256").update(directory).digest("hex").slice(0, 8)
    return `${dir}/${process.pid}-${instance}.sock`
  } catch (error) {
    logError(`no socket directory: ${error}`)
    return null
  }
}

async function runHook(event: string, input: Record<string, unknown>): Promise<Output> {
  try {
    const proc = Bun.spawn(resolved(["kaiusap", "hook", "opencode", event]), {
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
  } catch {
    return {}
  }
}

export const KaiusapPlugin = async ({ client, directory, serverUrl }: any) => {
  const created = new Map<string, Info>() // sessions this process created (session.created)
  const started = new Set<string>() // sessions it ran session.start for
  const prompted = new Map<string, string>() // root sessions that had a chat.message, with the latest's time
  let rootsStarted = 0
  const pid = Number(process.env.OPENCODE_PID ?? process.pid)
  let socket = socketPath(directory)

  async function info(id: string): Promise<Info | null> {
    if (created.has(id)) return created.get(id)!
    try {
      const result = await client.session.get({ path: { id } })
      return result?.data ? { parentID: result.data.parentID ?? null, title: result.data.title ?? null } : null
    } catch {
      return null
    }
  }

  // This process's OpenCode version, from its own `GET /global/health` (`{healthy, version}`), for `sessions`'
  // harness_version. Kept once found; asked again after a failure.
  let version: string | null = null
  async function runningVersion(): Promise<string | null> {
    if (version) return version
    try {
      const found = await client?._client?.get?.({ url: "/global/health" })
      if (typeof found?.data?.version === "string" && found.data.version) return (version = found.data.version)
    } catch {}
    try {
      if (serverUrl) {
        const body: any = await (await fetch(new URL("/global/health", serverUrl))).json()
        if (typeof body?.version === "string" && body.version) return (version = body.version)
      }
    } catch {}
    return null
  }

  async function title(id: string): Promise<string | null> {
    try {
      const result = await client.session.get({ path: { id } })
      return typeof result?.data?.title === "string" ? result.data.title : null
    } catch {
      return created.get(id)?.title ?? null
    }
  }

  async function rootOf(id: string): Promise<{ root: string; parent: string | null }> {
    let current = id
    let parent: string | null = null
    for (let hops = 0; hops < 32; hops++) {
      const found = await info(current)
      const up = found?.parentID ?? null
      if (hops === 0) parent = up
      if (!up) break
      current = up
    }
    return { root: current, parent }
  }

  function touched(id: string): boolean {
    return created.has(id) || started.has(id)
  }

  async function base(id: string): Promise<Record<string, unknown>> {
    const { root } = await rootOf(id)
    const input: Record<string, unknown> = {
      session_id: root, cwd: directory, pid, socket, tmux_pane: process.env.TMUX_PANE ?? null, title: await title(id),
    }
    if (root !== id) input.agent_id = id
    return input
  }

  // Before a session's first chat.message in this process, or a child session's first hook of any kind.
  async function ensureStarted(id: string): Promise<Output> {
    if (started.has(id)) return {}
    started.add(id)
    // session.created needs no wait: the event hook runs inline when Session.create publishes it
    // (core/src/event.ts:406-411, session/session.ts:535), so `created` is set before the first prompt.
    const found = await info(id)
    const parent = found?.parentID ?? null
    const input = await base(id)
    input.parent_id = parent
    input.created_here = created.has(id)
    input.roots_started_here = rootsStarted
    input.prompted_roots = [...prompted].map(([session_id, at]) => ({ session_id, at }))
    if (!parent) rootsStarted++
    return runHook("session.start", input)
  }

  async function apply(id: string, outputs: Output[], parts: any[] | null) {
    const told = outputs.map((o) => o.tell).filter((t): t is string => typeof t === "string" && t.length > 0)
    if (told.length && parts) {
      const template = parts[0] ?? {}
      parts.push({
        id: `${template.id ?? "prt_kaiusap"}k${parts.length}`, sessionID: template.sessionID ?? id,
        messageID: template.messageID, type: "text", text: told.join("\n"), synthetic: true,
      })
    }
    const named = outputs.map((o) => o.title).filter((t): t is string => typeof t === "string" && t.length > 0).pop()
    if (named) {
      try {
        await client.session.update({ path: { id }, body: { title: named } })
      } catch {}
    }
  }

  async function answer(request: any): Promise<Record<string, unknown>> {
    const id = typeof request?.session_id === "string" ? request.session_id : ""
    if (!id || !touched(id)) return { ok: false, code: "not_live" }
    if (request.type === "notice") {
      try {
        const result = await client.session.promptAsync({ path: { id }, body: { parts: [{ type: "text", text: String(request.text ?? "") }] } })
        if (result?.error) return { ok: false, code: "refused", message: JSON.stringify(result.error) }
        return { ok: true }
      } catch (error) {
        return { ok: false, code: "refused", message: String(error) }
      }
    }
    if (request.type === "rename") {
      try {
        const result = await client.session.update({ path: { id }, body: { title: String(request.name ?? "") } })
        return result?.error ? { ok: false, code: "refused", message: JSON.stringify(result.error) } : { ok: true }
      } catch (error) {
        return { ok: false, code: "refused", message: String(error) }
      }
    }
    if (request.type === "session") {
      const { root, parent } = await rootOf(id)
      return { ok: true, root, parent_id: parent, title: await title(id) }
    }
    if (request.type === "context") {
      try {
        const result = await client.session.messages({ path: { id } })
        const messages = (Array.isArray(result?.data) ? result.data : [])
          .map((m: any) => m?.info ?? m)
          .filter((m: any) => m?.role === "assistant")
          .slice(-50)
          .map((m: any) => ({ tokens: m.tokens ?? null, model: m.modelID ?? null, provider: m.providerID ?? null,
                              at: m.time?.completed ?? m.time?.created ?? null }))
        // Only the limits of the models these messages used: every configured provider's list made the reply
        // too long to send whole (reported 2026-11-12, a reply cut at 8192 bytes).
        const used = new Set(messages.map((m: any) => `${m.provider}/${m.model}`))
        const limits: Record<string, number> = {}
        try {
          const config = await client.config.providers()
          for (const provider of config?.data?.providers ?? []) {
            for (const [model, value] of Object.entries<any>(provider?.models ?? {})) {
              const limit = value?.limit?.context
              if (typeof limit === "number" && used.has(`${provider.id}/${model}`)) limits[`${provider.id}/${model}`] = limit
            }
          }
        } catch {}
        return { ok: true, messages, limits, version: await runningVersion() }
      } catch (error) {
        return { ok: false, code: "refused", message: String(error) }
      }
    }
    return { ok: false, code: "unknown_request" }
  }

  // A Bun socket's write sends what the kernel takes now and returns how much; the rest is written on `drain`.
  // The answer line is ended only once all of it is written, so a long reply arrives whole.
  function flush(conn: any) {
    const pending: Uint8Array | null = conn.data?.pending
    if (!pending) return
    const written = conn.write(pending)
    if (written < 0) { conn.data.pending = null; conn.end(); return }
    conn.data.pending = written < pending.length ? pending.subarray(written) : null
    if (!conn.data.pending) conn.end()
  }

  let server: any = null
  if (socket) {
    try {
      try { unlinkSync(socket) } catch {} // left by a killed process whose pid was reused
      server = Bun.listen({
        unix: socket,
        socket: {
          open(conn: any) { conn.data = { buffer: "", done: false, pending: null } },
          drain(conn: any) { flush(conn) },
          async data(conn: any, chunk: any) {
            if (conn.data.done) return
            conn.data.buffer += String(chunk)
            if (conn.data.buffer.length > MAX_LINE) { conn.data.done = true; conn.end(); return }
            const end = conn.data.buffer.indexOf("\n")
            if (end < 0) return
            conn.data.done = true
            let reply: Record<string, unknown>
            try {
              reply = await answer(JSON.parse(conn.data.buffer.slice(0, end)))
            } catch {
              reply = { ok: false, code: "bad_request" }
            }
            conn.data.pending = new TextEncoder().encode(JSON.stringify(reply) + "\n")
            flush(conn)
          },
        },
      })
    } catch (error) {
      logError(`can't listen on ${socket}: ${error}`)
      socket = null
    }
  }

  return {
    event: async ({ event }: any) => {
      if (event?.type === "session.created" || event?.type === "session.updated") {
        const session = event.properties?.info
        if (session?.id && (event.type === "session.created" || created.has(session.id))) {
          created.set(session.id, { parentID: session.parentID ?? null, title: session.title ?? null })
        }
      }
    },
    "chat.message": async (input: any, output: any) => {
      const id = input?.sessionID
      if (typeof id !== "string") return
      const outputs = [await ensureStarted(id)]
      const parts = Array.isArray(output?.parts) ? output.parts : null
      const prompt = (parts ?? []).filter((p: any) => p?.type === "text" && !p.synthetic).map((p: any) => p.text).join("\n")
      outputs.push(await runHook("chat.message", { ...(await base(id)), prompt }))
      const { root } = await rootOf(id)
      if (root === id) prompted.set(id, new Date().toISOString())
      await apply(id, outputs, parts)
    },
    "tool.execute.before": async (input: any, output: any) => {
      const tool = input?.tool
      const id = input?.sessionID
      if (typeof tool !== "string" || typeof id !== "string") return
      if (tool.startsWith(TOOL_PREFIX)) {
        // Overwrites whatever the model gave; a call that can't be stamped isn't sent with the model's value.
        if (!output?.args || typeof output.args !== "object") throw new Error("kaiusap couldn't mark this call with its session; it is refused.")
        output.args[IDENTITY_ARGUMENT] = id
      }
      if (!SCREENED_TOOLS.includes(tool)) return
      // A child's first hook may be this one: its start's title is applied; a tell has no prompt to go in.
      await apply(id, [await ensureStarted(id)], null)
      const { [IDENTITY_ARGUMENT]: _, ...args } = output?.args ?? {}
      const decided = await runHook("tool.execute.before", { ...(await base(id)), tool, args })
      if (typeof decided.deny === "string" && decided.deny) throw new Error(decided.deny)
    },
    "shell.env": async (input: any, output: any) => {
      if (typeof input?.sessionID === "string" && output?.env) output.env.KAIUSAP_OPENCODE_SESSION = input.sessionID
    },
    // On shutdown (plugin/index.ts's finalizer): stop serving, and leave no socket file behind.
    dispose: async () => {
      try { server?.stop(true) } catch {}
      if (socket) try { unlinkSync(socket) } catch {}
    },
  }
}
