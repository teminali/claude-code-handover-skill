---
name: handover
description: End a long session cleanly and hand it to a fresh chat. Writes a handover doc (goal, state, in-flight work, next steps, decisions, landmines) plus a paste-ready start-here prompt, and copies it to the clipboard. Use when the context guard fires AMBER/RED/CRITICAL, when context is large or the session feels slow or forgetful, when switching to an unrelated task, or when the user says "handover", "hand this over", "context is full", "start a new chat", "save tokens", "wrap up this session".
---

# Handover

Turn an expensive, context-heavy session into a cheap fresh one, losing nothing that matters.

A fresh session that reads a good handover doc starts at roughly 5-10k tokens instead of
inheriting 300-600k. That is the entire point: **the doc must carry the knowledge, not the
transcript.**

## When to run

- The context guard fired RED or CRITICAL (do it now, before any more tool calls).
- Guard fired AMBER and the user is about to start something new.
- The user is switching to an unrelated task — hand over, then `/clear`.
- You notice you are re-reading files you already read, or forgetting earlier decisions.

## Procedure

### 1. Stop

Do not start new work. Finish only a half-applied edit that would leave the tree broken.
Say in one line what you are stopping mid-way, if anything.

### 2. Gather the hard facts (one command, cheap)

```bash
python3 ~/.claude/handover/bin/ctx.py status
python3 ~/.claude/handover/bin/ctx.py facts
```

`facts` reads the transcript directly and gives you: every user prompt in order, files
edited and read, recent commands, the last todo list, git branch + uncommitted files +
recent commits, and tool-call counts. Use it — do not reconstruct this from memory, and do
not re-read project files to write the doc.

### 3. Write the body

Write to the scratchpad, e.g. `$SCRATCH/handover-body.md`. Use exactly these sections.

````markdown
## Goal
One paragraph. What the user actually wants, in their terms. Include the original ask and
how it evolved — from the prompt list in `facts`, not from your summary of it.

## Current state
What is DONE and verified working. Be specific: feature, file, and how it was confirmed
(build passed, screenshot checked, test green). Mark anything done-but-unverified.

## In flight
The exact stopping point. `path/to/file.ts:120` — what was being changed and why it is
not finished. Say "nothing in flight" if the tree is clean.

## Next steps
Ordered, concrete, each one actionable without further discovery. Not "improve the drawer"
but "in components/layout/app-sidebar.tsx, replace the More dialog footer buttons with a
segmented control; the palette tokens are --color-teal-* (re-pointed per business theme)".

## Decisions and constraints
The expensive knowledge — the reason a fresh session would otherwise repeat work. User
preferences stated in chat, rejected approaches, naming conventions, "the client wants X
not Y", API quirks discovered, why a workaround exists. This section is the most valuable
part of the doc.

## Landmines
What NOT to do. Approaches already tried that failed, files that look relevant but are
not, commands that break things, things the user explicitly rejected.

## Files that matter
Path + one line on its role. Paths only — never paste file contents.

## Verification
The exact commands that prove the work is good (build, typecheck, lint, test, dev server).
Include known-failing ones and whether the failures pre-date this work.

## Start-here prompt

```text
Continue work on <project>. Read the handover first: <ABSOLUTE_PATH_TO_DOC>

Short restatement of the goal and the single next action.

Do not re-explore the codebase; the handover lists the files that matter.
```
````

Rules for the body:

- **Never paste file contents, diffs, logs, or command output.** Reference `path:line`.
  A handover that inlines code is a handover that costs as much as the session it replaces.
- Write for someone with zero memory of this chat but full access to the repo.
- Aim for 60-150 lines. If it is longer, you are transcribing instead of summarising.
- Absolute paths, so the new session can open them directly.
- Leave the `## Start-here prompt` heading and its fenced block exactly as shown —
  the tooling extracts that block for the clipboard.

### 4. Finalise

```bash
python3 ~/.claude/handover/bin/ctx.py write --body "$SCRATCH/handover-body.md" \
  --cwd "$(pwd)" --title "short title"
```

This stamps machine, session, branch, model and context size onto the doc, saves it to
`<project>/.claude/handover/HANDOVER-<timestamp>.md`, updates `LATEST.md`, mirrors it to
the shared folder if one is configured (so another computer can pick it up), and copies
the start-here prompt to the clipboard.

### 5. Tell the user, briefly

Three lines, no more:

1. Where the doc is.
2. That the start-here prompt is on the clipboard.
3. `/clear` (same project) or a new chat window, then paste.

Then **stop**. Do not begin new work in this session.

## Picking a handover up

In the fresh session: read the doc, confirm the next step in one line, and start. Then mark
it taken so it stops being offered at session start:

```bash
python3 ~/.claude/handover/bin/ctx.py consume <path-to-handover.md>
```

## Other commands

| Command | Use |
|---|---|
| `ctx.py status` | context tokens, band, thresholds for this session |
| `ctx.py report --days 7` | where tokens actually went, across all local sessions |
| `ctx.py list` | handovers for this project, from every machine |
| `ctx.py show` | print the newest handover |
| `ctx.py doctor` | verify hooks, statusline, config, share dir |
| `ctx.py install` | wire the guard into settings.json (run once per machine) |
