# unwind

A web UI for Claude Code sessions. Run it in any project folder; a browser tab opens showing every Claude Code session that's run there, the conversation for each one, and — when the [callstack](https://github.com/amolk/agent-callstack) plugin's `/call` skill is used — the call hierarchy between sessions.

## Install

```bash
pip install unwind-labs
```

## Use

```bash
cd /path/to/your/claude-project
unwind
```

A browser tab opens at `http://127.0.0.1:<port>/` with that project's sessions. `Ctrl-C` to stop.

`unwind --help` lists flags (custom port, `--all` project picker, `--no-browser`, etc.).

## How it works

unwind never wraps or controls Claude Code. It reads:

- `~/.claude/projects/<slug>/*.jsonl` — Claude Code's own session logs
- `<project>/.claude/callstack/log/` — `/call` invocation reports written by the [callstack](https://github.com/amolk/agent-callstack) plugin

…and renders them in a live-updating web UI. Loopback only (`127.0.0.1`); read-only.

## More

- Architecture and design notes: [dev/PRD.md](dev/PRD.md), [dev/PLAN.md](dev/PLAN.md)
- License: [MIT](LICENSE)
