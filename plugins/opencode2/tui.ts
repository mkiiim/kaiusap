// kaiusap's OpenCode 2 plugin, the terminal part. OpenCode loads it in each
// terminal (TUI) from this directory's `tui` entry (`plugin/src/host.ts:57-58`).
//
// It reports which session this terminal shows: once when the terminal starts, each time that changes (a session
// id, or the home screen after `/new` or `/clear`), and when it quits. Each report runs `kaiusap hook opencode2
// tui.route`, which writes the terminal record and answers nothing. It holds no rule of kaiusap's.
import { runHook, runHookNow } from "./hook.ts"

const HOOK_EVENTS = ["tui.route"]
// `ctx.ui.router.current()` is read this often; a change is reported at once (whether the plugin can
// react to a route change through Solid instead is to check live).
const POLL_MS = 250

function shown(route: any): string | null {
  return route && route.type === "session" && typeof route.sessionID === "string" ? route.sessionID : null
}

export default {
  id: "kaiusap",
  setup: (ctx: any) => {
    const facts = () => ({ pid: process.pid, tmux_pane: process.env.TMUX_PANE ?? null, cwd: process.cwd() })
    let last: string | null | undefined = undefined
    let reporting = Promise.resolve()
    const report = () => {
      let route: any = null
      try {
        route = ctx.ui.router.current()
      } catch {}
      const shows = shown(route)
      if (shows === last || (route && route.type === "plugin")) return  // a plugin page changes nothing
      last = shows
      reporting = reporting.then(() => runHook("tui.route", { ...facts(), shows }).then(() => undefined))
    }
    report()
    const timer = setInterval(report, POLL_MS)
    let quit = false
    const leave = () => {
      if (quit) return
      quit = true
      clearInterval(timer)
      runHookNow("tui.route", { ...facts(), shows: null, quit: true })
    }
    process.once("exit", leave)
    return () => {
      process.off("exit", leave)
      leave()
    }
  },
}
