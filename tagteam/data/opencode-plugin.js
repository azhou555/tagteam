// tagteam: cross-agent review of OpenCode's turns. Written by `tagteam install`; `tagteam uninstall` removes it.
// Each hook pipes a JSON payload to the tagteam command below and acts on its JSON reply. Any failure is ignored.
import { spawn } from "node:child_process"

const HOOKS = __HOOKS__
const TIMEOUT_S = __TIMEOUT_S__
const MARKER = "[tagteam]"
const WRITES = new Set(["edit", "write", "patch", "apply_patch", "multiedit"])

function call(phase, payload) {
  return new Promise((resolve) => {
    let out = ""
    let child
    try {
      child = spawn("/bin/sh", ["-c", HOOKS[phase]], { stdio: ["pipe", "pipe", "ignore"] })
    } catch {
      return resolve({})
    }
    const timer = setTimeout(() => child.kill("SIGKILL"), TIMEOUT_S[phase] * 1000)
    child.stdout.on("data", (d) => (out += d))
    child.on("error", () => resolve({}))
    child.on("close", () => {
      clearTimeout(timer)
      try {
        resolve(JSON.parse(out || "{}"))
      } catch {
        resolve({})
      }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

// `opencode run` without --attach exits when the session goes idle, so a turn-end review there has nowhere to land.
function headless() {
  const args = process.argv.slice(1)
  if (args.length && /[\\/]/.test(args[0])) args.shift()
  return args.find((a) => !a.startsWith("-")) === "run"
}

// OpenCode 1.x calls server() and uses the hooks it returns.
async function server({ client, directory }) {
  if (headless() || process.env.TAGTEAM_DEPTH) return {}
  const roots = new Map()
  const turns = new Map()
  const picks = new Map()     // the agent and model of the user's last prompt; a fix round runs with the same
  const aborted = new Set()   // stopped by the user: like Claude's Esc, the turn ends unreviewed and nothing restarts it

  // A subagent's session reports as its root's: its edits belong to the root's turn, and only the root's turn ends.
  async function root(id) {
    if (!roots.has(id)) {
      let parent
      try {
        parent = (await client.session.get({ path: { id } }))?.data?.parentID
      } catch {}
      roots.set(id, parent ? await root(parent) : id)
    }
    return roots.get(id)
  }

  async function notice(reply) {
    if (!reply.systemMessage) return
    try {
      await client.tui.showToast({ body: { message: reply.systemMessage, variant: "info" } })
    } catch {}
  }

  async function turnEnd(id) {
    const turn = turns.get(id)
    const reply = await call("turn-end", { sessionID: id, cwd: directory })
    await notice(reply)
    if (reply.decision !== "block" || !reply.reason || turns.get(id) !== turn) return   // the user moved on
    await client.session.promptAsync({ path: { id },
                                       body: { ...picks.get(id), parts: [{ type: "text", text: reply.reason }] } })
  }

  return {
    "chat.message": async (input, output) => {
      try {
        const id = input.sessionID
        if ((await root(id)) !== id) return
        const prompt = output.parts.filter((p) => p.type === "text" && !p.synthetic).map((p) => p.text).join("\n")
        aborted.delete(id)
        if (!prompt.trimStart().startsWith(MARKER)) {
          turns.set(id, (turns.get(id) || 0) + 1)
          picks.set(id, Object.fromEntries(Object.entries({ agent: input.agent, model: input.model }).filter(([, v]) => v)))
        }
        const reply = await call("turn-start", { sessionID: id, cwd: directory, prompt })
        await notice(reply)
        const context = reply.hookSpecificOutput?.additionalContext
        const last = output.parts.at(-1)
        if (context && last?.id && last.messageID)     // a part missing any of these fails the user's whole turn
          output.parts.push({ id: `${last.id}tt`, sessionID: id, messageID: last.messageID, type: "text",
                              text: context, synthetic: true })
      } catch {}
    },
    "tool.execute.after": async (input) => {
      try {
        if (!WRITES.has(input.tool)) return
        await call("touch", { sessionID: await root(input.sessionID), cwd: directory, tool: input.tool, args: input.args })
      } catch {}
    },
    event: async ({ event }) => {
      const id = event.properties?.sessionID
      if (event.type === "session.error" && event.properties?.error?.name === "MessageAbortedError") aborted.add(id)
      if (event.type !== "session.idle" || !id || aborted.has(id)) return     // an abort is followed by two idles
      try {
        if ((await root(id)) === id) turnEnd(id).catch(() => {})     // not awaited: the review can take minutes
      } catch {}
    },
  }
}

// OpenCode 2.x calls setup(). A fix round sent with session.prompt keeps the session's own agent and model. A server
// plugin has no toast here, so notices are dropped.
async function setup(ctx) {
  if (process.env.TAGTEAM_DEPTH) return
  const directory = ctx.location.directory
  const roots = new Map()
  const turns = new Map()

  async function root(id) {
    if (!roots.has(id)) {
      let parent
      try {
        parent = (await ctx.session.get({ sessionID: id }))?.parentID
      } catch {}
      roots.set(id, parent ? await root(parent) : id)
    }
    return roots.get(id)
  }

  async function turnEnd(id) {
    const turn = turns.get(id)
    const reply = await call("turn-end", { sessionID: id, cwd: directory })
    if (reply.decision !== "block" || !reply.reason || turns.get(id) !== turn) return   // the user moved on
    await ctx.session.prompt({ sessionID: id, text: reply.reason })
  }

  await ctx.session.hook("prompt", async (event) => {
    try {
      const id = event.sessionID
      if ((await root(id)) !== id) return
      const prompt = event.prompt.text
      if (!prompt.trimStart().startsWith(MARKER)) turns.set(id, (turns.get(id) || 0) + 1)
      const reply = await call("turn-start", { sessionID: id, cwd: directory, prompt })
      const context = reply.hookSpecificOutput?.additionalContext
      if (context) await ctx.session.synthetic({ sessionID: id, text: context, resume: false })   // lands before the prompt
    } catch {}
  })
  await ctx.tool.hook("execute.after", async (event) => {
    try {
      if (!WRITES.has(event.tool)) return
      await call("touch", { sessionID: await root(event.sessionID), cwd: directory, tool: event.tool, args: event.input })
    } catch {}
  })
  // An interrupted turn (the user stopped it) ends unreviewed, like Claude's Esc; the next prompt is reviewed again.
  const stop = new AbortController()
  ;(async () => {
    const types = ["session.execution.succeeded", "session.execution.failed"]
    for await (const event of ctx.event.subscribe({ signal: stop.signal, types })) {
      const id = event.data?.sessionID
      try {
        if (id && (await root(id)) === id) turnEnd(id).catch(() => {})     // not awaited: the review can take minutes
      } catch {}
    }
  })().catch(() => {})
  return () => stop.abort()
}

export default { id: "tagteam", setup, server }
