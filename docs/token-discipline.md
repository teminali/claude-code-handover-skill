# Token discipline

Append this to `~/.claude/CLAUDE.md` so it applies to every session.
It is the behavioural half of this repo; the guard is the enforcement half.

```
Context is re-sent on every turn, so a large session is expensive on every turn,
not just once. Keep it small.

• READ NARROW. Use `grep -n`, `sed -n 'A,Bp'`, `head`, `rg -C3` to pull the lines
  you need. Read a whole file only when you will actually change most of it.
• NEVER re-read a file already in this context. It is still there.
• CAP OUTPUT. Pipe long command output through `head`/`tail`/`grep`. Never `cat`
  a build log, lockfile, minified bundle, or anything over ~300 lines.
• BATCH independent tool calls into one message instead of one per turn.
• SUBAGENTS each run their own request stream — spawn one only when the task is
  genuinely parallel or would otherwise dump a lot of junk into this context.
• DON'T QUOTE BACK file contents, diffs, or logs in your replies. Cite `path:line`.
• AT TASK BOUNDARIES, when switching to unrelated work, run the `handover` skill
  and tell the user to `/clear` rather than carrying the old context forward.
• The context guard fires AMBER ~110k, RED ~160k, CRITICAL ~220k tokens. Treat RED
  as a hard stop on new work: hand over, don't push through.
  Check any time with: `python3 ~/.claude/handover/bin/ctx.py status`
```
