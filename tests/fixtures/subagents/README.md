# Subagent hook captures (2026-11-24)

Live headless runs with a logging hook on every event. `_t` is the hook's receive time and `_phase` is
the event. Local paths are rewritten to `/repo` and `~`. What each one shows is in
`docs/attribution-redesign.md` under "Hook payloads".

- `claude-subagents.jsonl` — Claude Code: foreground, two parallel, worktree (`isolation: worktree`) and
  background subagents, plus a root Bash call. The root waits for the background agent.
- `claude-background-subagent.jsonl` — the root stops while a background subagent is still running. The
  subagent writes after that Stop, and a `<task-notification>` turn follows (F2).
- `copilot-subagent.jsonl` — Copilot CLI 1.0.114: one `task` subagent that runs as its own session with
  `parentAgentId` (F6).
- `agy-subagent.jsonl` — agy 1.5.16: one `invoke_subagent` subagent with its own `conversationId` and no
  parent field (F6).
