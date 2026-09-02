#!/usr/bin/env bash
# Install the Claude Code context guard + handover skill.
#
#   git clone https://github.com/teminali/claude-code-handover-skill.git
#   cd claude-code-handover-skill && ./install.sh
#
# Idempotent: safe to re-run to update. Backs up settings.json before touching it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HOME/.claude/handover"
SKILL="$HOME/.claude/skills/handover"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

if [ "$SRC" != "$ROOT" ]; then
  mkdir -p "$ROOT/bin" "$ROOT/state" "$SKILL"
  cp "$SRC/bin/ctx.py"      "$ROOT/bin/ctx.py"
  cp "$SRC/skill/SKILL.md"  "$SKILL/SKILL.md"
  [ -f "$ROOT/config.json" ] || cp "$SRC/config.example.json" "$ROOT/config.json"
  echo "installed files -> $ROOT and $SKILL"
fi

chmod +x "$ROOT/bin/ctx.py"
python3 "$ROOT/bin/ctx.py" install "$@"

# Share handovers between machines through iCloud Drive, if it exists here.
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/claude-handovers"
if [ -d "$(dirname "$ICLOUD")" ]; then
  mkdir -p "$ICLOUD"
  python3 - "$ICLOUD" <<'PY'
import json, pathlib, sys
p = pathlib.Path.home()/".claude/handover/config.json"
c = json.loads(p.read_text()) if p.exists() else {}
if not c.get("share_dir"):
    c["share_dir"] = sys.argv[1]
    p.write_text(json.dumps(c, indent=2)+"\n")
    print("share_dir ->", sys.argv[1])
PY
fi

echo
python3 "$ROOT/bin/ctx.py" doctor || true
echo
echo "Done. Open /hooks once (or restart Claude Code) so the hooks load."
echo "Optional: append docs/token-discipline.md to ~/.claude/CLAUDE.md"
