// OpenCode plugin — blocking barrier for background subagents (V2).
//
// V2 starts every subagent in the background: the `subagent` call returns a
// sessionID immediately, and the parent is free to continue before the child has
// finished. The old 1.x blocking semantics cannot be restored by configuration,
// so this plugin restores them as an explicit tool the parent can call:
//
//   await_subagents({ sessionIDs: [...] })
//
// It returns only when every listed child is idle, or on timeout/cancel, and
// prints a per-session ledger so a count can never stand in for a completion.
//
// V1 has no `ctx.tool` context, so `setup` is a no-op there and the tool simply
// does not exist (V1's task calls already block).

function text(s) {
  return { content: s }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function waitOne(sessionID, { timeoutMs, signal, ctx, POLL_MS }) {
  const started = Date.now()
  const deadline = timeoutMs > 0 ? started + timeoutMs : 0
  let status = "idle"
  let waitPromise

  // One read up front: a session that was never spawned is reported as missing
  // instead of being waited on forever.
  let info = await safeGet(sessionID, ctx)
  if (!info) {
    return { sessionID, status: "missing", outcome: null, title: null, elapsedMs: Date.now() - started, stillRunning: null }
  }

  for (;;) {
    if (signal?.aborted) {
      status = "aborted"
      break
    }
    if (deadline && Date.now() >= deadline) {
      status = "timeout"
      break
    }

    // One wait per session, reused across slices: re-issuing the wait each
    // slice would restart the harness's own wait and starve a slow child.
    waitPromise ??= ctx.session.wait({ sessionID }).then(
      () => "idle",
      () => "error",
    )

    // Wait in slices so abort and timeout stay responsive. The sleep always
    // resolves, so no immortal timer survives this function.
    const slice = deadline ? Math.max(50, Math.min(POLL_MS, deadline - Date.now())) : POLL_MS
    const result = await Promise.race([waitPromise, sleep(slice).then(() => "tick")])

    if (result === "idle") {
      // A child can be woken again by a follow-up round: confirm before leaving.
      const confirmed = await safeGet(sessionID, ctx)
      if (!confirmed) {
        status = "missing"
        break
      }
      if (!confirmed.busy) break
    }
  }

  info = await safeGet(sessionID, ctx)
  return {
    sessionID,
    status,
    outcome: info?.outcome ?? null,
    title: info?.title ?? null,
    elapsedMs: Date.now() - started,
    stillRunning: info ? Boolean(info.busy) : null,
  }
}

async function safeGet(sessionID, ctx) {
  try {
    const s = await ctx.session.get({ sessionID })
    if (!s) return null
    // time.busy / time.idle are timestamps: a session is running when the busy
    // stamp is newer than the idle stamp (or no idle stamp exists yet).
    const busyAt = typeof s.time?.busy === "number" ? s.time.busy : undefined
    const idleAt = typeof s.time?.idle === "number" ? s.time.idle : undefined
    const busy = busyAt !== undefined && (idleAt === undefined || busyAt > idleAt)
    return { outcome: s.outcome ?? null, title: s.title ?? null, busy }
  } catch (_) {
    return null
  }
}

export default {
  id: "subagent-barrier",

  async setup(ctx) {
    if (!ctx?.tool?.transform) return // V1: no V2 tool context

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "await_subagents",
        description:
          "Block until every listed subagent session is idle, then return a per-session ledger. " +
          "Call this after spawning subagents and before starting any stage that consumes their reports: " +
          "a stage is complete only when every issued spawn appears in the ledger as status=idle. " +
          "Never treat a report file, a count of notifications, or a clean git status as completion. " +
          "status=missing means that sessionID was never spawned or is gone; status=timeout means it is still running.",
        input: {
          type: "object",
          properties: {
            sessionIDs: {
              type: "array",
              items: { type: "string" },
              minItems: 1,
              description: "Session IDs returned by the subagent calls in this batch.",
            },
            timeoutMs: {
              type: "integer",
              minimum: 0,
              description:
                "Optional wall-clock limit. 0 or omitted waits until every child is idle.",
            },
          },
          required: ["sessionIDs"],
          additionalProperties: false,
        },
        options: { codemode: true },
        execute: async (input, context) => {
          const ids = Array.isArray(input?.sessionIDs) ? input.sessionIDs.filter((x) => typeof x === "string" && x) : []
          if (!ids.length) return text("await_subagents: no sessionIDs given; nothing waited for. all_idle: false")
          const timeoutMs = Number.isFinite(input?.timeoutMs) ? Math.max(0, Number(input.timeoutMs)) : 0
          const POLL_MS = 1000

          const results = await Promise.all(
            ids.map((id) => waitOne(id, { timeoutMs, signal: context?.signal, ctx, POLL_MS })),
          )

          const idle = results.filter((r) => r.status === "idle").length
          const lines = results.map((r, i) => {
            const parts = [
              `${i + 1}. ${r.sessionID}`,
              `status=${r.status}`,
              `outcome=${r.outcome ?? "unknown"}`,
              `waited=${Math.round(r.elapsedMs / 1000)}s`,
            ]
            if (r.stillRunning) parts.push("still_running=true")
            if (r.title) parts.push(`title=${JSON.stringify(r.title)}`)
            return parts.join("  ")
          })

          return text(
            `await_subagents: ${idle}/${results.length} idle — all_idle: ${idle === results.length}\n` +
              lines.join("\n"),
          )
        },
      })
    })
  },
}
