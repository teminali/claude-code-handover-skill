# Claude Code handover skill

Claude Code sessions grow until they are expensive. Every turn re-sends the whole
conversation, so a session sitting at 300k tokens pays for 300k tokens *again* on
every single turn — long after most of that context stopped being useful.

This repo adds two things:

1. **A context guard** — hooks that watch the live session and warn, then stop, as
   context grows.
2. **A `/handover` skill** — turns the dying session into a short document plus a
   paste-ready prompt, so a fresh session picks the work up at a fraction of the cost.

It is stdlib Python and shell. No dependencies, no network calls, no telemetry.

## The problem, measured

`ctx.py report` over three days on one developer's machine:

```
Context report - last 3 days, 34 sessions, 4825 model turns

  tokens re-sent as context : 1,514.9M
  turns above 150k context  : 3407 (71%)
  avg context per turn      : 314k

  project                    peak ctx  turns  >150k   re-sent
  project-a                      919k   1227   1081    577.0M
  project-a                      672k    704    611    269.7M
  project-b                      611k    668    555    227.9M
```

Nearly three quarters of all turns were paying for more than 150k tokens of context.
One session peaked at 919k. Almost none of that was work in progress — it was
transcript.

## Install

```bash
git clone https://github.com/teminali/claude-code-handover-skill.git
cd claude-code-handover-skill
./install.sh
```

Then open `/hooks` once (or restart Claude Code) so the hooks load. Verify with:

```bash
python3 ~/.claude/handover/bin/ctx.py doctor
```

`install.sh` copies the engine to `~/.claude/handover/`, the skill to
`~/.claude/skills/handover/`, and merges three hooks plus a status line into
`~/.claude/settings.json` — after backing it up. It never overwrites an existing
status line unless you pass `--force-statusline`.

## What you get

**A live status line**

```
my-project | Opus 5 | ctx 171k [######..] 86% /handover | $2.41
```

**Escalating intervention.** Bands are absolute token counts, because cost tracks
absolute context, not percentage of the window:

| Band | Default | What happens |
|---|---|---|
| AMBER | 110k | Claude is told to finish the current step and open nothing new |
| RED | 160k | Claude is told to stop new work and run `/handover` |
| CRITICAL | 220k | The tool call is blocked and the handover is forced |

Each band fires once per session and re-arms if context drops after a `/clear` or a
compaction. Between checks the guard costs one `stat()` call — it only re-reads the
transcript once it has grown by 40 KB.

**`/handover`.** Claude writes a document covering the goal, what is done, what is
in flight, ordered next steps, the decisions and constraints that took the whole
session to discover, landmines, the files that matter, and how to verify the work.
The start-here prompt lands on your clipboard. Paste it into a new session.

The skill's hard rule: reference `path:line`, never paste file contents. A handover
that inlines code costs as much as the session it replaces.

**Savings, measured not guessed.** Every handover ends with:

```
| Context carried by this session   |  214,448 |               |
| A fresh session seeded by this doc |  38,769 |               |
| Avoided on every future turn      |  175,679 | $0.088/turn   |
| Over the next 20 turns            | 3,513,580| $1.76         |

This session has already re-sent 10.5M tokens of context across 79 turns (~$5.26).
```

The baseline is read from the session's own first request, not assumed. Dollar
figures are list price at the cache-read rate — on a subscription plan the real
currency is your usage allowance, and the token counts are the honest number.

## What it actually saved

`savings` projects forward. `savings --all` looks backwards and measures what the
handovers you have already written did save:

```
$ python3 ~/.claude/handover/bin/ctx.py savings --all

Realized handover savings - 4 of 8 handovers picked up

  project          written     ctx@handover fresh start  turns     saved
  project-a        09-02 16:22      228,897      39,889    136     25.7M
  project-b        09-02 16:34      623,451      41,769    199    115.8M
  project-a        09-02 18:01      177,181      40,056    139     19.1M
  project-a        09-02 18:24      186,487      43,214    126     18.1M

  tokens not re-sent : 178.6M
  at list price      : $89.29 (cache-read rate)
```

It reads every handover doc it can reach — this project, the share folder, and every
project directory any local transcript was recorded in — pulls `context_at_handover`
from the frontmatter, then finds the fresh session that picked each one up and reads
that session's own first request as the real baseline. Saving is
`(context_at_handover - fresh_start) x turns since`.

Two rules keep the number honest:

- A session counts as a pickup only if it names the doc **within its first few turns**
  (`--window`, default 3). A session that merely mentions a handover later — one
  analysing them, for instance — is not counted.
- Each fresh session is credited **once**, to the newest doc it read, so a chain of
  superseded handovers cannot bill the same turns twice.

Anything it cannot trace is listed separately rather than assumed. `ctx.py consume`
now stamps the consuming session id into the doc, which makes attribution exact
instead of heuristic. Add `--json` for a machine-readable version, `--days N` to
limit the window.

The total is a floor: it assumes the old session's context would have stayed flat at
its handover size, when in reality it kept growing every turn.

## Commands

```bash
ctx=~/.claude/handover/bin/ctx.py
python3 $ctx status            # context size and band for this session
python3 $ctx savings           # what a handover would save, right now
python3 $ctx savings --all     # what the handovers you already wrote did save
python3 $ctx report --days 7   # where tokens actually went, across all sessions
python3 $ctx facts             # prompts, files, commands, todos, git state
python3 $ctx list              # handovers for this project, from every machine
python3 $ctx show              # print the newest handover
python3 $ctx consume <file>    # mark one as picked up
python3 $ctx doctor            # verify the install
```

## More than one computer

A hook only runs inside its own session, so nothing here can watch a chat running on
a different machine. What travels is the toolkit and the handover documents.

Run `./install.sh` on each machine. Handovers are written into the project at
`.claude/handover/` **and** mirrored to a shared folder, tagged with the machine that
wrote them. On macOS the installer defaults that folder to iCloud Drive; any synced
directory works — set `share_dir` in `~/.claude/handover/config.json` to a Dropbox
folder, a git repo, an SMB share.

The `SessionStart` hook then offers a pending handover from *either* machine when you
open a session in that project.

For a repo shared with a team, or for cloud sessions, install project-scoped hooks so
the guard travels with the code:

```bash
python3 ~/.claude/handover/bin/ctx.py install --project /path/to/repo
```

## Configuration

`~/.claude/handover/config.json` (see `config.example.json`):

| Key | Default | Meaning |
|---|---|---|
| `thresholds` | 110k / 160k / 220k | amber, red, critical |
| `pct_of_window` | 0.70 / 0.85 | percentage trips, applied only to a *known* window |
| `block_at_critical` | `true` | actually block the tool call at critical |
| `projection_turns` | 20 | turns the savings projection assumes |
| `share_dir` | `""` | shared handover folder for multi-machine use |
| `pricing` | Opus/Sonnet/Haiku list | verify against anthropic.com/pricing |
| `enabled` | `true` | set `false` to switch the whole guard off |

Transcripts record `claude-opus-5` for both the 200k and 1M variants of a model, so
the context window is only treated as *known* once a session passes 200k. Until then
the absolute thresholds do the work and no percentage trip can falsely escalate. Pin
it with `assume_window` if you always use one model.

## Also worth doing

`docs/token-discipline.md` is a block to append to `~/.claude/CLAUDE.md`: read narrow,
never re-read a file already in context, cap command output, batch tool calls, be
deliberate about subagents, cite `path:line` instead of quoting files. The guard is
enforcement; that file is prevention.

## Uninstall

```bash
python3 -c "import json,pathlib;p=pathlib.Path.home()/'.claude/handover/config.json';c=json.loads(p.read_text());c['enabled']=False;p.write_text(json.dumps(c,indent=2))"
```

Or restore a backup: `~/.claude/settings.json.bak-*`.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
