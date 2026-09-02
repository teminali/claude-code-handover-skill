#!/usr/bin/env python3
"""
ctx.py - Claude Code context guard + session handover toolkit.

Portable: copy the whole ~/.claude/handover folder to any machine,
run `python3 bin/ctx.py install`, and that machine gets the same behaviour.

Subcommands:
  guard         hook entry (PostToolUse / UserPromptSubmit) - stdin JSON -> hook JSON
  sessionstart  hook entry (SessionStart) - surfaces a pending handover
  statusline    statusLine command - stdin JSON -> one status line
  status        human-readable context reading for the current session
  facts         extract hard facts from a transcript for a handover doc
  write         finalise a handover doc (adds metadata, mirrors, clipboard)
  list          list recent handovers (all machines, if a share dir is set)
  show          print a handover doc
  consume       mark a handover as picked up
  report        token-waste report across local transcripts
  install       merge hooks + statusLine into a settings.json
  doctor        verify the install
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1.0"
HOME = Path.home()
ROOT = Path(os.environ.get("CLAUDE_HANDOVER_ROOT", str(HOME / ".claude" / "handover")))
STATE_DIR = ROOT / "state"
CONFIG_PATH = ROOT / "config.json"
PROJECTS = HOME / ".claude" / "projects"
MACHINE = socket.gethostname().split(".")[0]

DEFAULT_CONFIG = {
    "enabled": True,
    # absolute context-token thresholds - cost scales with absolute context,
    # not with percentage of the window
    "thresholds": {"amber": 110000, "red": 160000, "critical": 220000},
    # ...but also trip relative to a small window (200k models)
    "pct_of_window": {"red": 0.70, "critical": 0.85},
    "block_at_critical": True,
    "big_tool_result_tokens": 25000,
    "min_growth_bytes": 40000,   # skip re-reading transcript until it grows this much
    "share_dir": "",             # e.g. ~/Library/Mobile Documents/com~apple~CloudDocs/claude-handovers
    "extra_transcript_dirs": [],
    "clipboard": True,
    "assume_window": 0,   # 0 = auto-detect; pin to 1000000 or 200000 to be explicit
    "projection_turns": 20,   # turns the session would plausibly have continued
    "pricing": {
        # USD per 1M tokens, Anthropic list price. Verify at anthropic.com/pricing.
        # Cache read is 0.1x input; a 5m cache write is 1.25x input.
        "cache_read_multiplier": 0.1,
        "cache_write_multiplier": 1.25,
        "models": {
            "claude-opus-5":    {"input": 5.0,  "output": 25.0},
            "claude-opus-4-8":  {"input": 5.0,  "output": 25.0},
            "claude-fable-5":   {"input": 10.0, "output": 50.0},
            "claude-sonnet-5":  {"input": 2.0,  "output": 10.0},
            "claude-haiku-4-5": {"input": 1.0,  "output": 5.0},
        },
        "fallback": {"input": 5.0, "output": 25.0},
    },
}

BANDS = ["green", "amber", "red", "critical"]


# ----------------------------------------------------------------- config ---
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# ------------------------------------------------------------- transcript ---
def tail_bytes(path: Path, nbytes: int) -> list[bytes]:
    size = path.stat().st_size
    start = max(0, size - nbytes)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    if start > 0:
        i = data.find(b"\n")
        data = data[i + 1:] if i >= 0 else b""
    return data.split(b"\n")


def read_context(path: Path) -> dict | None:
    """Latest main-thread context size, from the newest assistant usage record."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    for nb in (262144, 1048576, 4194304, 16777216, 67108864):
        for raw in reversed(tail_bytes(path, nb)):
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            tok = ((u.get("input_tokens") or 0)
                   + (u.get("cache_read_input_tokens") or 0)
                   + (u.get("cache_creation_input_tokens") or 0))
            if tok <= 0:
                continue
            return {
                "tokens": tok,
                "model": msg.get("model") or "",
                "ts": d.get("timestamp") or "",
                "cwd": d.get("cwd") or "",
                "session_id": d.get("sessionId") or "",
                "git_branch": d.get("gitBranch") or "",
            }
        if nb >= size:
            break
    return None


def window_for(model: str, observed: int, cfg: dict | None = None) -> int:
    return window_detail(model, observed, cfg)[0]


def window_detail(model: str, observed: int, cfg: dict | None = None) -> tuple[int, bool]:
    """(window, known). Transcripts record `claude-opus-5` even for the 1M variant, so
    the window is only *known* once context has passed 200k or it is pinned in config."""
    env = os.environ.get("CLAUDE_CTX_WINDOW")
    if env and env.isdigit():
        return int(env), True
    pinned = (cfg or {}).get("assume_window") or 0
    if pinned:
        return int(pinned), True
    if "[1m]" in model or observed > 200000:
        return 1000000, True
    return 200000, False


def band_for(tokens: int, window: int, cfg: dict, window_known: bool = True) -> str:
    """Absolute thresholds lead; the percentage trip only applies to a window we
    actually know, so an unidentified 1M session is never falsely escalated."""
    t = cfg["thresholds"]
    p = cfg["pct_of_window"]
    if window_known:
        crit = min(t["critical"], int(window * p["critical"]))
        red = min(t["red"], int(window * p["red"]))
    else:
        crit, red = t["critical"], t["red"]
    amber = min(t["amber"], red - 1)
    if tokens >= crit:
        return "critical"
    if tokens >= red:
        return "red"
    if tokens >= amber:
        return "amber"
    return "green"


def find_transcript(cwd: str | None, session_id: str | None) -> Path | None:
    """Resolve a transcript. The running session's own id is exported by Claude Code,
    which is the only reliable answer when several sessions share a project dir."""
    cands: list[Path] = []
    ids = [session_id,
           os.environ.get("CLAUDE_CODE_SESSION_ID"),
           os.environ.get("CLAUDE_SESSION_ID")]
    for sid in [i for i in ids if i]:
        for d in PROJECTS.glob("*"):
            p = d / f"{sid}.jsonl"
            if p.exists():
                return p
    if cwd:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        d = PROJECTS / slug
        if d.is_dir():
            cands = list(d.glob("*.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------------ state ---
def state_path(session_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return STATE_DIR / f"{safe}.json"


def load_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except Exception:
        return {}


def save_state(session_id: str, st: dict) -> None:
    try:
        state_path(session_id).write_text(json.dumps(st))
    except Exception:
        pass


# ------------------------------------------------------------------ utils ---
def fmt_tok(n: int) -> str:
    if n >= 1000000:
        return f"{n / 1000000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def project_slug(cwd: str) -> str:
    return Path(cwd).name or "root"


def handover_dir(cwd: str) -> Path:
    d = Path(cwd) / ".claude" / "handover"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        d = HOME / ".claude" / "handover" / "docs" / project_slug(cwd)
        d.mkdir(parents=True, exist_ok=True)
        return d


def share_dir(cfg: dict) -> Path | None:
    s = (cfg.get("share_dir") or "").strip()
    if not s:
        return None
    p = Path(os.path.expanduser(s))
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


# ------------------------------------------------------------------ guard ---
GUARD_TEXT = {
    "amber": (
        "CONTEXT GUARD - AMBER ({tok} tokens in context, ~{cost}x the cost of a fresh turn).\n"
        "Finish the step you are on. Do not open new workstreams, do not spawn subagents, "
        "and prefer targeted `grep`/`sed -n` over reading whole files. "
        "If a new task is coming, say so and offer to run the `handover` skill first."
    ),
    "red": (
        "CONTEXT GUARD - RED ({tok} tokens in context). Every further turn re-sends all of it.\n"
        "STOP starting new work now. Complete only the edit in flight, then invoke the "
        "`handover` skill (Skill tool, skill: \"handover\"). It writes a handover doc plus a "
        "paste-ready prompt so a fresh session can continue at a fraction of the cost. "
        "Do not read more files, run broad searches, or spawn subagents before that."
    ),
    "critical": (
        "CONTEXT GUARD - CRITICAL ({tok} tokens in context). This turn is expensive and quality degrades.\n"
        "HARD STOP on new work. Do not run further exploratory tools. Immediately invoke the "
        "`handover` skill (Skill tool, skill: \"handover\") to write the handover doc and the "
        "new-chat prompt, tell the user to start a fresh session, and end the turn."
    ),
}


def cmd_guard(payload: dict) -> int:
    cfg = load_config()
    if not cfg.get("enabled", True):
        return 0

    event = payload.get("hook_event_name") or "PostToolUse"
    session_id = payload.get("session_id") or ""
    tpath = payload.get("transcript_path")
    path = Path(tpath) if tpath else find_transcript(payload.get("cwd"), session_id)
    if not path or not path.exists():
        return 0

    st = load_state(session_id)
    size = path.stat().st_size

    # cheap escape hatch: don't re-read the transcript until it has grown enough
    if event == "PostToolUse":
        last_size = st.get("last_size", 0)
        if size - last_size < cfg.get("min_growth_bytes", 40000):
            return 0

    info = read_context(path)
    st["last_size"] = size
    if not info:
        save_state(session_id, st)
        return 0

    tokens = info["tokens"]
    window, known = window_detail(info["model"], tokens, cfg)
    band = band_for(tokens, window, cfg, known)

    prev_tokens = st.get("tokens", 0)
    fired = st.get("fired", [])
    # context dropped a lot -> compaction or /clear: re-arm every band
    if prev_tokens and tokens < prev_tokens * 0.7:
        fired = []
    st["tokens"] = tokens
    st["band"] = band
    st["window"] = window
    st["updated"] = time.time()

    if band == "green" or band in fired:
        st["fired"] = fired
        save_state(session_id, st)
        return 0

    fired.append(band)
    st["fired"] = fired
    save_state(session_id, st)

    ratio = max(1, round(tokens / 15000))
    text = GUARD_TEXT[band].format(tok=f"{tokens:,}", cost=ratio)
    pct = 100.0 * tokens / window
    ui = f"context {fmt_tok(tokens)}/{fmt_tok(window)} ({pct:.0f}%) - {band.upper()}"
    if band == "amber":
        ui += " - wrap up the current step"
    elif band == "red":
        ui += " - /handover recommended"
    else:
        ui += " - HANDOVER NOW"

    out: dict = {
        "systemMessage": ui,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text},
    }
    if band == "critical" and cfg.get("block_at_critical", True) and event == "PostToolUse":
        out["decision"] = "block"
        out["reason"] = text
    emit(out)
    return 0


# ----------------------------------------------------------- session start ---
def cmd_sessionstart(payload: dict) -> int:
    cfg = load_config()
    cwd = payload.get("cwd") or os.getcwd()
    docs = pending_handovers(cfg, cwd, max_age_hours=48)
    if not docs:
        return 0
    d = docs[0]
    age_h = (time.time() - d["mtime"]) / 3600.0
    note = (
        f"A session handover is waiting for this project: {d['path']} "
        f"(written {age_h:.0f}h ago on machine '{d['machine']}'). "
        "If this session continues that work, read it first and then run "
        "`python3 ~/.claude/handover/bin/ctx.py consume " + d["path"] + "`. "
        "If this is unrelated new work, ignore it."
    )
    emit({
        "systemMessage": f"handover pending: {Path(d['path']).name}",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note},
    })
    return 0


def pending_handovers(cfg: dict, cwd: str, max_age_hours: float = 48) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    dirs = [handover_dir(cwd)]
    sd = share_dir(cfg)
    if sd:
        dirs.append(sd / project_slug(cwd))
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob("HANDOVER-*.md"):
            if p.name in seen:
                continue
            try:
                head = p.read_text(errors="replace")[:1200]
            except Exception:
                continue
            if re.search(r"^status:\s*consumed", head, re.M):
                continue
            age = (time.time() - p.stat().st_mtime) / 3600.0
            if age > max_age_hours:
                continue
            m = re.search(r"^machine:\s*(\S+)", head, re.M)
            seen.add(p.name)
            out.append({"path": str(p), "mtime": p.stat().st_mtime,
                        "machine": m.group(1) if m else "?"})
    out.sort(key=lambda x: -x["mtime"])
    return out


# ------------------------------------------------------------- status line ---
C_RESET = "\x1b[0m"
C_DIM = "\x1b[2m"
C_GREEN = "\x1b[32m"
C_YELLOW = "\x1b[33m"
C_RED = "\x1b[31m"
C_BOLD_RED = "\x1b[1;31m"
BAND_COLOR = {"green": C_GREEN, "amber": C_YELLOW, "red": C_RED, "critical": C_BOLD_RED}


def bar(frac: float, width: int = 8) -> str:
    filled = max(0, min(width, round(frac * width)))
    return "#" * filled + "." * (width - filled)


def cmd_statusline(payload: dict) -> int:
    cfg = load_config()
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or ""
    model = ((payload.get("model") or {}).get("display_name")
             or (payload.get("model") or {}).get("id") or "")
    tpath = payload.get("transcript_path")
    path = Path(tpath) if tpath else find_transcript(cwd, payload.get("session_id"))

    parts = [f"{C_DIM}{Path(cwd).name or '~'}{C_RESET}"]
    if model:
        parts.append(f"{C_DIM}{model}{C_RESET}")

    info = read_context(path) if path and path.exists() else None
    if info:
        tokens = info["tokens"]
        window, known = window_detail(info["model"], tokens, cfg)
        band = band_for(tokens, window, cfg, known)
        col = BAND_COLOR[band]
        frac = tokens / window
        seg = f"{col}ctx {fmt_tok(tokens)} [{bar(frac)}] {100*frac:.0f}%{C_RESET}"
        if band == "amber":
            seg += f" {C_YELLOW}wrap up{C_RESET}"
        elif band == "red":
            seg += f" {C_RED}/handover{C_RESET}"
        elif band == "critical":
            seg += f" {C_BOLD_RED}HANDOVER NOW{C_RESET}"
        parts.append(seg)

    cost = payload.get("cost") or {}
    usd = cost.get("total_cost_usd")
    if isinstance(usd, (int, float)) and usd > 0:
        parts.append(f"{C_DIM}${usd:.2f}{C_RESET}")

    sys.stdout.write(f" {C_DIM}|{C_RESET} ".join(parts))
    return 0


# ----------------------------------------------------------------- status ---
def cmd_status(args: list[str]) -> int:
    cfg = load_config()
    as_json = "--json" in args
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
    path = None
    for i, a in enumerate(args):
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
        if a == "--session" and i + 1 < len(args):
            path = find_transcript(cwd, args[i + 1])
    if path is None:
        path = find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd)
        return 1
    info = read_context(path)
    if not info:
        print("no usage records yet in", path)
        return 1
    tokens = info["tokens"]
    window, known = window_detail(info["model"], tokens, cfg)
    band = band_for(tokens, window, cfg, known)
    t = cfg["thresholds"]
    if as_json:
        print(json.dumps({"tokens": tokens, "window": window, "band": band,
                          "pct": round(100 * tokens / window, 1),
                          "model": info["model"], "transcript": str(path),
                          "thresholds": t}))
        return 0
    print(f"transcript : {path}")
    print(f"model      : {info['model'] or '?'}   window {fmt_tok(window)}"
          + ("" if known else "  (assumed - pin with assume_window in config.json)"))
    print(f"context    : {tokens:,} tokens  ({100*tokens/window:.1f}% of window)")
    print(f"band       : {band.upper()}   [amber {t['amber']:,} | red {t['red']:,} | critical {t['critical']:,}]")
    print(f"file size  : {path.stat().st_size/1048576:.1f} MB")
    return 0


# ------------------------------------------------------------------ facts ---
TOOL_FILE_KEYS = ("file_path", "notebook_path", "path")


def scan_transcript(path: Path, max_bytes: int = 40 * 1024 * 1024) -> dict:
    """Cheap single pass. Returns hard facts for a handover doc."""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    truncated = start > 0
    prompts: list[str] = []
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    bash: list[str] = []
    todos: list[dict] = []
    tool_counts: dict[str, int] = {}
    subagents = 0
    turns = 0
    first_ts = last_ts = ""
    cwd = branch = ""

    with open(path, "rb") as f:
        if start:
            f.seek(start)
            f.readline()
        for raw in f:
            if len(raw) < 8:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            ts = d.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = d.get("cwd") or cwd
            branch = d.get("gitBranch") or branch
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            content = msg.get("content")

            if t == "user" and not d.get("isMeta"):
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content
                                     if isinstance(b, dict) and b.get("type") == "text")
                text = text.strip()
                if (text and not text.startswith("<") and not text.startswith("Caveat:")
                        and "system-reminder" not in text[:60]):
                    prompts.append(text)
            elif t == "assistant":
                turns += 1
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name") or "?"
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                        inp = b.get("input") or {}
                        if name in ("Agent", "Task"):
                            subagents += 1
                        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
                            todos = inp["todos"]
                        if name == "Bash" and isinstance(inp.get("command"), str):
                            bash.append(inp["command"].strip().replace("\n", " ")[:160])
                        fp = next((inp[k] for k in TOOL_FILE_KEYS
                                   if isinstance(inp.get(k), str)), None)
                        if fp:
                            bucket = writes if name in ("Edit", "Write", "NotebookEdit") else reads
                            bucket[fp] = bucket.get(fp, 0) + 1

    info = read_context(path) or {}
    return {
        "transcript": str(path), "truncated": truncated,
        "size_mb": round(size / 1048576, 1),
        "cwd": cwd, "branch": branch,
        "first_ts": first_ts, "last_ts": last_ts,
        "prompts": prompts, "reads": reads, "writes": writes,
        "bash": bash, "todos": todos, "tools": tool_counts,
        "subagents": subagents, "turns": turns,
        "context_tokens": info.get("tokens", 0), "model": info.get("model", ""),
    }


def _git(cwd: str, argv: list[str]) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd] + argv, capture_output=True,
                           text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def cmd_facts(args: list[str]) -> int:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = None
    as_json = "--json" in args
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
    if path is None:
        path = find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd, file=sys.stderr)
        return 1
    f = scan_transcript(path)
    if as_json:
        print(json.dumps(f, indent=2)[:200000])
        return 0

    def top(d: dict, n: int) -> list[tuple[str, int]]:
        return sorted(d.items(), key=lambda kv: -kv[1])[:n]

    print(f"# Session facts\n")
    print(f"- transcript: `{f['transcript']}` ({f['size_mb']} MB"
          + (", tail-scanned" if f["truncated"] else "") + ")")
    print(f"- cwd: `{f['cwd']}`" + (f"  branch: `{f['branch']}`" if f["branch"] else ""))
    print(f"- window: {f['first_ts'][:19]} -> {f['last_ts'][:19]}")
    print(f"- assistant turns: {f['turns']}   subagents: {f['subagents']}   "
          f"context now: {f['context_tokens']:,} tokens")
    if f["tools"]:
        print("- tool calls: " + ", ".join(f"{k} x{v}" for k, v in top(f["tools"], 10)))

    if f["writes"]:
        print("\n## Files modified")
        for p, n in top(f["writes"], 30):
            print(f"- `{p}` ({n} edit{'s' if n > 1 else ''})")
    if f["reads"]:
        print("\n## Files read (top)")
        for p, n in top(f["reads"], 20):
            print(f"- `{p}` (x{n})")
    if f["todos"]:
        print("\n## Last todo list")
        for td in f["todos"]:
            mark = {"completed": "x", "in_progress": "~"}.get(td.get("status"), " ")
            print(f"- [{mark}] {td.get('content') or td.get('activeForm') or ''}")
    if f["bash"]:
        print("\n## Recent commands")
        seen: set[str] = set()
        out = []
        for c in reversed(f["bash"]):
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            if len(out) >= 15:
                break
        for c in reversed(out):
            print(f"- `{c}`")
    gcwd = f["cwd"] or cwd
    if (Path(gcwd) / ".git").exists() or _git(gcwd, ["rev-parse", "--is-inside-work-tree"]):
        st = _git(gcwd, ["status", "--short"])
        lg = _git(gcwd, ["log", "--oneline", "-5"])
        print("\n## Git state")
        print(f"- repo: `{gcwd}`  branch: `{_git(gcwd, ['rev-parse', '--abbrev-ref', 'HEAD']) or '?'}`")
        if st:
            print("- uncommitted:")
            for line in st.splitlines()[:25]:
                print(f"    {line}")
        else:
            print("- working tree clean")
        if lg:
            print("- recent commits:")
            for line in lg.splitlines():
                print(f"    {line}")

    if f["prompts"]:
        print("\n## User prompts, in order (verbatim, truncated)")
        for p in f["prompts"][-25:]:
            one = " ".join(p.split())
            print(f"- {one[:300]}")
    return 0


# ---------------------------------------------------------------- savings ---
def rates_for(model: str, cfg: dict) -> dict:
    pr = cfg.get("pricing") or DEFAULT_CONFIG["pricing"]
    models = pr.get("models") or {}
    base = None
    for key, val in models.items():
        if model and (model == key or model.startswith(key)):
            base = val
            break
    base = base or pr.get("fallback") or {"input": 5.0, "output": 25.0}
    return {
        "input": base["input"],
        "output": base["output"],
        "cache_read": base["input"] * pr.get("cache_read_multiplier", 0.1),
        "cache_write": base["input"] * pr.get("cache_write_multiplier", 1.25),
    }


def session_usage(path: Path) -> dict:
    """Measured, not estimated: baseline start cost and everything re-sent so far."""
    baseline = 0
    resent = 0
    written = 0
    out = 0
    turns = 0
    peak = 0
    with open(path, "rb") as f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            u = (d.get("message") or {}).get("usage") or {}
            cr = u.get("cache_read_input_tokens") or 0
            cw = u.get("cache_creation_input_tokens") or 0
            tot = (u.get("input_tokens") or 0) + cr + cw
            if tot <= 0:
                continue
            turns += 1
            if baseline == 0:
                baseline = tot
            resent += cr
            written += cw
            out += u.get("output_tokens") or 0
            peak = max(peak, tot)
    return {"baseline": baseline, "resent": resent, "written": written,
            "output": out, "turns": turns, "peak": peak}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def compute_savings(path: Path, cfg: dict, doc_text: str = "", turns: int | None = None) -> dict:
    u = session_usage(path)
    info = read_context(path) or {}
    ctx_now = info.get("tokens", u["peak"])
    model = info.get("model", "")
    r = rates_for(model, cfg)
    n = turns if turns is not None else int(cfg.get("projection_turns", 20))

    doc_tokens = estimate_tokens(doc_text) if doc_text else 0
    fresh_start = u["baseline"] + doc_tokens
    avoided_per_turn = max(0, ctx_now - fresh_start)
    avoided_total = avoided_per_turn * n

    usd = lambda tok, rate: tok * rate / 1_000_000
    return {
        "model": model,
        "context_now": ctx_now,
        "baseline": u["baseline"],
        "doc_tokens": doc_tokens,
        "fresh_start": fresh_start,
        "avoided_per_turn": avoided_per_turn,
        "projection_turns": n,
        "avoided_total": avoided_total,
        "avoided_usd": usd(avoided_total, r["cache_read"]),
        "spent_resent": u["resent"],
        "spent_resent_usd": usd(u["resent"], r["cache_read"]),
        "session_usd": (usd(u["resent"], r["cache_read"])
                        + usd(u["written"], r["cache_write"])
                        + usd(u["output"], r["output"])),
        "turns": u["turns"],
        "rate_cache_read": r["cache_read"],
    }


def render_savings(sv: dict) -> str:
    L = []
    L.append("## Savings from this handover")
    L.append("")
    L.append("| | tokens | at list price |")
    L.append("|---|---:|---:|")
    L.append(f"| Context carried by this session | {sv['context_now']:,} | |")
    L.append(f"| A fresh session seeded by this doc | {sv['fresh_start']:,} | |")
    L.append(f"| **Avoided on every future turn** | **{sv['avoided_per_turn']:,}** | "
             f"**${sv['avoided_per_turn']*sv['rate_cache_read']/1e6:,.3f}/turn** |")
    L.append(f"| Over the next {sv['projection_turns']} turns | "
             f"{sv['avoided_total']:,} | ${sv['avoided_usd']:,.2f} |")
    L.append("")
    L.append(f"This session has already re-sent **{sv['spent_resent']/1e6:,.1f}M tokens** "
             f"of context across {sv['turns']} turns "
             f"(~${sv['spent_resent_usd']:,.2f}); total session cost ~${sv['session_usd']:,.2f}.")
    L.append("")
    L.append(f"_Projection, not a bill. Measured baseline {sv['baseline']:,} tokens + "
             f"{sv['doc_tokens']:,} for this doc; avoided tokens valued at the cache-read rate "
             f"(${sv['rate_cache_read']:.2f}/M) for {sv['model'] or 'this model'}. "
             f"On a subscription the real currency is your usage allowance, not dollars._")
    return "\n".join(L)


def cmd_savings(args: list[str]) -> int:
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    doc = ""
    turns = None
    path = None
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--doc" and i + 1 < len(args) and Path(args[i + 1]).exists():
            doc = Path(args[i + 1]).read_text()
        if a == "--turns" and i + 1 < len(args):
            turns = int(args[i + 1])
        if a == "--transcript" and i + 1 < len(args):
            path = Path(args[i + 1])
    path = path or find_transcript(cwd, None)
    if not path or not path.exists():
        print("no transcript found for", cwd, file=sys.stderr)
        return 1
    print(render_savings(compute_savings(path, cfg, doc, turns)))
    return 0


# ------------------------------------------------------------------ write ---
FRONTMATTER = """---
handover: {title}
project: {project}
cwd: {cwd}
branch: {branch}
machine: {machine}
session: {session}
model: {model}
context_at_handover: {tokens}
written: {when}
status: pending
---

"""


def cmd_write(args: list[str]) -> int:
    cfg = load_config()
    body_file = None
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    title = "session handover"
    for i, a in enumerate(args):
        if a == "--body" and i + 1 < len(args):
            body_file = Path(args[i + 1])
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
        if a == "--title" and i + 1 < len(args):
            title = args[i + 1]
    if not body_file or not body_file.exists():
        print("usage: ctx.py write --body <file.md> [--cwd DIR] [--title T]", file=sys.stderr)
        return 1

    body = body_file.read_text()
    tpath = find_transcript(cwd, None)
    info = (read_context(tpath) if tpath and tpath.exists() else None) or {}
    savings = None
    if tpath and tpath.exists():
        try:
            savings = compute_savings(tpath, cfg, body)
            body = body.rstrip() + "\n\n" + render_savings(savings) + "\n"
        except Exception:
            savings = None
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    doc = FRONTMATTER.format(
        title=title, project=project_slug(cwd), cwd=cwd,
        branch=info.get("git_branch") or "-", machine=MACHINE,
        session=(info.get("session_id") or "-")[:8], model=info.get("model") or "-",
        tokens=info.get("tokens", 0), when=now_iso(),
    ) + body.strip() + "\n"

    d = handover_dir(cwd)
    out = d / f"HANDOVER-{stamp}.md"
    out.write_text(doc)
    (d / "LATEST.md").write_text(doc)
    written = [str(out)]

    sd = share_dir(cfg)
    if sd:
        sub = sd / project_slug(cwd)
        sub.mkdir(parents=True, exist_ok=True)
        mirror = sub / f"HANDOVER-{stamp}-{MACHINE}.md"
        mirror.write_text(doc)
        written.append(str(mirror))

    # pull the start-here prompt out of the first fenced block after the heading
    prompt = ""
    m = re.search(r"#+\s*Start-?here prompt.*?```(?:\w+)?\n(.*?)```", doc, re.S | re.I)
    if m:
        prompt = m.group(1).strip()
    if prompt:
        (d / "PROMPT.txt").write_text(prompt + "\n")
        written.append(str(d / "PROMPT.txt"))
        if cfg.get("clipboard", True) and sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=prompt.encode(), timeout=5)
                written.append("(copied to clipboard)")
            except Exception:
                pass
    for w in written:
        print(w)
    if savings:
        print()
        print(render_savings(savings))
    return 0


def cmd_list(args: list[str]) -> int:
    cfg = load_config()
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for i, a in enumerate(args):
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
    docs = pending_handovers(cfg, cwd, max_age_hours=24 * 365)
    if not docs:
        print("no handovers for", project_slug(cwd))
        return 0
    for d in docs[:20]:
        age = (time.time() - d["mtime"]) / 3600.0
        print(f"{age:6.1f}h ago  [{d['machine']:<12}] {d['path']}")
    return 0


def cmd_show(args: list[str]) -> int:
    cfg = load_config()
    if args and Path(args[0]).exists():
        print(Path(args[0]).read_text())
        return 0
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    docs = pending_handovers(cfg, cwd, max_age_hours=24 * 365)
    if not docs:
        print("no handovers for", project_slug(cwd))
        return 1
    print(Path(docs[0]["path"]).read_text())
    return 0


def cmd_consume(args: list[str]) -> int:
    if not args:
        print("usage: ctx.py consume <handover.md>", file=sys.stderr)
        return 1
    p = Path(args[0])
    if not p.exists():
        print("not found:", p, file=sys.stderr)
        return 1
    txt = p.read_text()
    txt = re.sub(r"^status:\s*pending", f"status: consumed by {MACHINE} at {now_iso()}",
                 txt, count=1, flags=re.M)
    p.write_text(txt)
    print("marked consumed:", p)
    return 0


# ----------------------------------------------------------------- report ---
def cmd_report(args: list[str]) -> int:
    days = 7
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
    cutoff = time.time() - days * 86400
    dirs = [PROJECTS] + [Path(os.path.expanduser(d))
                         for d in load_config().get("extra_transcript_dirs", [])]
    rows = []
    for base in dirs:
        if not base.is_dir():
            continue
        for p in base.glob("*/*.jsonl"):
            try:
                stt = p.stat()
            except OSError:
                continue
            if stt.st_mtime < cutoff:
                continue
            peak = 0
            cache_read = 0
            turns = 0
            heavy = 0
            with open(p, "rb") as f:
                for raw in f:
                    if b'"cache_read_input_tokens"' not in raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    u = (d.get("message") or {}).get("usage") or {}
                    cr = u.get("cache_read_input_tokens") or 0
                    tot = ((u.get("input_tokens") or 0) + cr
                           + (u.get("cache_creation_input_tokens") or 0))
                    if tot <= 0:
                        continue
                    turns += 1
                    cache_read += cr
                    peak = max(peak, tot)
                    if tot > 150000:
                        heavy += 1
            if turns:
                rows.append({"path": p, "project": p.parent.name, "peak": peak,
                             "resent": cache_read, "turns": turns, "heavy": heavy,
                             "mtime": stt.st_mtime})
    if not rows:
        print(f"no transcripts in the last {days} days")
        return 0
    rows.sort(key=lambda r: -r["resent"])
    total_resent = sum(r["resent"] for r in rows)
    total_turns = sum(r["turns"] for r in rows)
    total_heavy = sum(r["heavy"] for r in rows)
    print(f"Context report - last {days} days, {len(rows)} sessions, {total_turns} model turns\n")
    print(f"  tokens re-sent as context : {total_resent/1e6:,.1f}M")
    print(f"  turns above 150k context  : {total_heavy} ({100*total_heavy/max(1,total_turns):.0f}%)")
    print(f"  avg context per turn      : {fmt_tok(int(total_resent/max(1,total_turns)))}\n")
    print(f"  {'project':<34} {'peak ctx':>9} {'turns':>6} {'>150k':>6} {'re-sent':>9}")
    for r in rows[:15]:
        proj = r["project"].replace("-Users-teminali-Documents-my-projects-", "")[:34]
        print(f"  {proj:<34} {fmt_tok(r['peak']):>9} {r['turns']:>6} {r['heavy']:>6} "
              f"{r['resent']/1e6:>8.1f}M")
    print("\n  Sessions with many >150k turns are the ones to hand over earlier.")
    return 0


# ---------------------------------------------------------------- install ---
CMD = '"$HOME/.claude/handover/bin/ctx.py"'
HOOK_SPECS = {
    "PostToolUse": ("guard", "context guard"),
    "UserPromptSubmit": ("guard", "context guard"),
    "SessionStart": ("sessionstart", "handover check"),
}


def cmd_install(args: list[str]) -> int:
    project = None
    force_status = "--force-statusline" in args
    for i, a in enumerate(args):
        if a == "--project" and i + 1 < len(args):
            project = Path(args[i + 1])
    target = (project / ".claude" / "settings.json") if project else (HOME / ".claude" / "settings.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    settings = {}
    if target.exists():
        raw = target.read_text()
        try:
            settings = json.loads(raw)
        except Exception:
            print(f"ERROR: {target} is not valid JSON - fix it first", file=sys.stderr)
            return 1
        backup = target.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(raw)
        print(f"backup: {backup}")

    hooks = settings.setdefault("hooks", {})
    for event, (sub, label) in HOOK_SPECS.items():
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries
                      if not any("ctx.py" in (h.get("command") or "")
                                 for h in (e.get("hooks") or []))]
        entries.append({"hooks": [{
            "type": "command",
            "command": f"python3 {CMD} {sub}",
            "timeout": 10,
            "statusMessage": label,
        }]})

    if force_status or "statusLine" not in settings:
        settings["statusLine"] = {"type": "command",
                                  "command": f"python3 {CMD} statusline",
                                  "padding": 0}
    else:
        print("note: statusLine already set, left alone (use --force-statusline to replace)")

    target.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"installed into {target}")
    print("hooks: " + ", ".join(HOOK_SPECS))
    print("open /hooks once (or restart Claude Code) to load them")
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        print(f"wrote default config: {CONFIG_PATH}")
    return 0


def cmd_doctor(args: list[str]) -> int:
    ok = True
    print(f"ctx.py {VERSION} on {MACHINE}  (python {sys.version.split()[0]})")
    print(f"root       : {ROOT}  {'OK' if ROOT.is_dir() else 'MISSING'}")
    cfg = load_config()
    print(f"config     : {CONFIG_PATH}  {'OK' if CONFIG_PATH.exists() else 'defaults'}")
    print(f"thresholds : amber {cfg['thresholds']['amber']:,} | red {cfg['thresholds']['red']:,}"
          f" | critical {cfg['thresholds']['critical']:,}")
    sd = share_dir(cfg)
    print(f"share dir  : {sd if sd else '(not set - single machine only)'}")
    st = HOME / ".claude" / "settings.json"
    try:
        s = json.loads(st.read_text())
    except Exception:
        print(f"settings   : {st} UNREADABLE")
        return 1
    for event in HOOK_SPECS:
        found = any("ctx.py" in (h.get("command") or "")
                    for e in (s.get("hooks", {}).get(event) or [])
                    for h in (e.get("hooks") or []))
        print(f"hook {event:<16}: {'wired' if found else 'MISSING'}")
        ok = ok and found
    sl = (s.get("statusLine") or {}).get("command", "")
    print(f"statusLine : {'wired' if 'ctx.py' in sl else 'not using ctx.py'}")
    cwd = os.getcwd()
    p = find_transcript(cwd, None)
    print(f"transcript : {p if p else 'none found for ' + cwd}")
    if p:
        info = read_context(p)
        if info:
            w, kn = window_detail(info["model"], info["tokens"], cfg)
            print(f"context    : {info['tokens']:,} tokens -> {band_for(info['tokens'], w, cfg, kn).upper()}")
    return 0 if ok else 1


# ------------------------------------------------------------------- main ---
def read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "guard":
            return cmd_guard(read_stdin_json())
        if cmd == "sessionstart":
            return cmd_sessionstart(read_stdin_json())
        if cmd == "statusline":
            return cmd_statusline(read_stdin_json())
        if cmd == "status":
            return cmd_status(args)
        if cmd == "facts":
            return cmd_facts(args)
        if cmd == "write":
            return cmd_write(args)
        if cmd == "list":
            return cmd_list(args)
        if cmd == "show":
            return cmd_show(args)
        if cmd == "consume":
            return cmd_consume(args)
        if cmd == "savings":
            return cmd_savings(args)
        if cmd == "report":
            return cmd_report(args)
        if cmd == "install":
            return cmd_install(args)
        if cmd == "doctor":
            return cmd_doctor(args)
    except BrokenPipeError:
        return 0
    except Exception as e:
        # a hook must never break the session
        if cmd in ("guard", "sessionstart", "statusline"):
            return 0
        print(f"ctx.py {cmd}: {e}", file=sys.stderr)
        return 1
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
