// kaiusap's OpenCode 2 plugin, the service part. OpenCode's service loads it once
// for each directory (Location) it serves, from this directory's `server` entry (`plugin/src/host.ts:57-58`).
//
// It holds no rule of kaiusap's. For each event in HOOK_EVENTS it runs `kaiusap hook opencode2 <event>` with the
// hook input on stdin and carries out the output: `tell` is added as a system part to the model requests of the
// turn the prompt began (in the `context` hook, not stored or shown), `deny` is an error thrown from
// `execute.before`, which the model sees as the tool's error. The session's title is set by kaiusap itself.
import { runHook } from "./hook.ts"

const HOOK_EVENTS = ["session.start", "session.prompt", "tool.execute.before"]
const SCREENED_TOOLS = ["shell", "kaiusap_whoami", "kaiusap_who", "kaiusap_iam", "kaiusap_release", "kaiusap_send", "kaiusap_reply", "kaiusap_inbox", "kaiusap_read", "kaiusap_moot", "kaiusap_pause", "kaiusap_unpause", "kaiusap_help"]

type Info = { id?: string; parentID?: string | null; fork?: { sessionID?: string } | null; title?: string | null; location?: { directory?: string } | null }

export default {
  id: "kaiusap",
  setup: async (ctx: any) => {
    const started = new Set<string>() // sessions this plugin ran session.start for, since it loaded
    const told = new Map<string, { messageID: string; text: string }>() // a turn's told text, by session

    async function info(id: string): Promise<Info | null> {
      try {
        const found = await ctx.session.get({ sessionID: id })
        return found && typeof found === "object" ? (found.data ?? found) : null
      } catch {
        return null
      }
    }

    // The root and the session's own facts; a child is named by its root and its own id.
    async function base(id: string): Promise<{ input: Record<string, unknown>; own: Info | null }> {
      const own = await info(id)
      let root = id
      let current = own
      for (let hops = 0; hops < 32 && current?.parentID; hops++) {
        root = current.parentID
        current = await info(root)
      }
      const input: Record<string, unknown> = { session_id: root, cwd: own?.location?.directory ?? process.cwd() }
      if (root !== id) input.agent_id = id
      input.title = typeof own?.title === "string" ? own.title : null
      return { input, own }
    }

    async function history(id: string): Promise<boolean> {
      try {
        const found = await ctx.session.context({ sessionID: id })
        const messages = Array.isArray(found) ? found : found?.data
        return Array.isArray(messages) && messages.length > 0
      } catch {
        return false
      }
    }

    await ctx.session.hook("prompt", async (event: any) => {
      const id = String(event.sessionID)
      const { input, own } = await base(id)
      if (!started.has(id)) {
        started.add(id)
        // The prompt this start comes with, so a notice that is the session's first prompt counts as told.
        const output = await runHook("session.start", {
          ...input, parent_id: own?.parentID ?? null, fork_of: own?.fork?.sessionID ?? null,
          has_history: await history(id), prompt: String(event.prompt?.text ?? ""),
        })
        if (output.tell) told.set(id, { messageID: String(event.messageID), text: output.tell })
      }
      const metadata = event.metadata && typeof event.metadata === "object" ? event.metadata : null
      const output = await runHook("session.prompt", { ...input, prompt: String(event.prompt?.text ?? ""), metadata })
      if (output.tell) {
        const held = told.get(id)
        const text = held && held.messageID === String(event.messageID) ? `${held.text}\n\n${output.tell}` : output.tell
        told.set(id, { messageID: String(event.messageID), text })
      }
    })

    await ctx.session.hook("context", (event: any) => {
      const held = told.get(String(event.sessionID))
      if (!held) return
      // Only the requests of the turn that prompt began: its message is the newest user message.
      const users = (event.messages ?? []).filter((m: any) => m?.role === "user")
      const newest = users.length ? users[users.length - 1] : null
      if (!newest || newest.id !== held.messageID) {
        told.delete(String(event.sessionID))
        return
      }
      event.system.push({ type: "text", text: held.text })
    })

    await ctx.tool.hook("execute.before", async (event: any) => {
      if (!SCREENED_TOOLS.includes(String(event.tool))) return
      const { input } = await base(String(event.sessionID))
      const args = event.input && typeof event.input === "object" ? event.input : {}
      const output = await runHook("tool.execute.before", { ...input, tool: String(event.tool), args })
      if (output.deny) throw new Error(output.deny)
    })
  },
}
