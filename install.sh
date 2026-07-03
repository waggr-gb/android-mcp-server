#!/usr/bin/env bash
# install.sh — register the Android MCP server with every LLM client installed
# on this machine (Claude Code, Claude Desktop, Cursor, Windsurf, Gemini CLI,
# Codex CLI, VS Code). Idempotent: re-running updates the existing entry.
#
# Usage:
#   ./install.sh            # uv sync + register with all detected clients
#   ./install.sh --no-sync  # skip `uv sync`
#   ./install.sh --name NAME  # register under NAME (default: android)
set -uo pipefail

SERVER_NAME="android"
RUN_SYNC=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-sync) RUN_SYNC=0 ;;
    --name) SERVER_NAME="${2:?--name requires a value}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "error: uv is required but not on PATH." >&2
  echo "Install it first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [ "$RUN_SYNC" -eq 1 ]; then
  echo "==> uv sync ($REPO_DIR)"
  (cd "$REPO_DIR" && "$UV_BIN" sync) || { echo "error: uv sync failed" >&2; exit 1; }
fi

# The stdio command every client will launch.
CMD="$UV_BIN"
ARGS_JSON="[\"--directory\", \"$REPO_DIR\", \"run\", \"server.py\"]"
SERVER_JSON="{\"command\": \"$CMD\", \"args\": $ARGS_JSON}"

PY_BIN="$(command -v python3 || true)"
[ -z "$PY_BIN" ] && PY_BIN="$UV_BIN run --directory $REPO_DIR python"

INSTALLED=()
SKIPPED=()
FAILED=()

# merge_json <config-file> <top-level-key> [payload-json]
# Inserts/replaces <top-level-key>.<SERVER_NAME> = payload (default SERVER_JSON),
# preserving the rest of the file. Creates the file (and parent dirs) if missing.
merge_json() {
  local file="$1" key="$2" payload="${3:-$SERVER_JSON}"
  [ -f "$file" ] && cp "$file" "$file.bak"
  $PY_BIN - "$file" "$key" "$SERVER_NAME" "$payload" <<'PYEOF'
import json, os, sys
path, key, name, payload = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
data = {}
if os.path.exists(path):
    with open(path) as f:
        text = f.read().strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"could not parse {path} ({e}); add the entry manually\n")
            sys.exit(2)
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
data.setdefault(key, {})[name] = payload
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
}

ok()   { echo "  [ok] $1"; INSTALLED+=("$1"); }
skip() { echo "  [--] $1 (not installed)"; SKIPPED+=("$1"); }
fail() { echo "  [!!] $1 — $2" >&2; FAILED+=("$1"); }

echo "==> Registering MCP server '$SERVER_NAME' ($CMD --directory $REPO_DIR run server.py)"

# --- Claude Code (CLI) ------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user "$SERVER_NAME" >/dev/null 2>&1 || true
  if claude mcp add --scope user "$SERVER_NAME" -- "$CMD" --directory "$REPO_DIR" run server.py >/dev/null 2>&1; then
    ok "Claude Code"
  else
    fail "Claude Code" "'claude mcp add' failed; run it manually"
  fi
else
  skip "Claude Code"
fi

# --- Claude Desktop ---------------------------------------------------------
case "$OS" in
  Darwin) CLAUDE_DESKTOP_DIR="$HOME/Library/Application Support/Claude" ;;
  *)      CLAUDE_DESKTOP_DIR="$HOME/.config/Claude" ;;
esac
if [ -d "$CLAUDE_DESKTOP_DIR" ]; then
  if merge_json "$CLAUDE_DESKTOP_DIR/claude_desktop_config.json" "mcpServers"; then
    ok "Claude Desktop"
  else
    fail "Claude Desktop" "could not update claude_desktop_config.json"
  fi
else
  skip "Claude Desktop"
fi

# --- Cursor -----------------------------------------------------------------
if [ -d "$HOME/.cursor" ]; then
  if merge_json "$HOME/.cursor/mcp.json" "mcpServers"; then
    ok "Cursor"
  else
    fail "Cursor" "could not update ~/.cursor/mcp.json"
  fi
else
  skip "Cursor"
fi

# --- Windsurf ---------------------------------------------------------------
if [ -d "$HOME/.codeium/windsurf" ]; then
  if merge_json "$HOME/.codeium/windsurf/mcp_config.json" "mcpServers"; then
    ok "Windsurf"
  else
    fail "Windsurf" "could not update mcp_config.json"
  fi
else
  skip "Windsurf"
fi

# --- Gemini CLI -------------------------------------------------------------
if command -v gemini >/dev/null 2>&1 || [ -d "$HOME/.gemini" ]; then
  if merge_json "$HOME/.gemini/settings.json" "mcpServers"; then
    ok "Gemini CLI"
  else
    fail "Gemini CLI" "could not update ~/.gemini/settings.json"
  fi
else
  skip "Gemini CLI"
fi

# --- Codex CLI --------------------------------------------------------------
if command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ]; then
  if command -v codex >/dev/null 2>&1 && codex mcp add --help >/dev/null 2>&1; then
    codex mcp remove "$SERVER_NAME" >/dev/null 2>&1 || true
    if codex mcp add "$SERVER_NAME" -- "$CMD" --directory "$REPO_DIR" run server.py >/dev/null 2>&1; then
      ok "Codex CLI"
    else
      fail "Codex CLI" "'codex mcp add' failed; add [mcp_servers.$SERVER_NAME] to ~/.codex/config.toml manually"
    fi
  else
    CODEX_TOML="$HOME/.codex/config.toml"
    codex_has_entry() {
      [ -f "$CODEX_TOML" ] || return 1
      while IFS= read -r line; do
        [ "$line" = "[mcp_servers.$SERVER_NAME]" ] && return 0
      done < "$CODEX_TOML"
      return 1
    }
    if codex_has_entry; then
      echo "  [ok] Codex CLI (already configured in config.toml — left as is)"
      INSTALLED+=("Codex CLI")
    else
      mkdir -p "$HOME/.codex"
      [ -f "$CODEX_TOML" ] && cp "$CODEX_TOML" "$CODEX_TOML.bak"
      {
        [ -s "$CODEX_TOML" ] && echo ""
        echo "[mcp_servers.$SERVER_NAME]"
        echo "command = \"$CMD\""
        echo "args = [\"--directory\", \"$REPO_DIR\", \"run\", \"server.py\"]"
      } >> "$CODEX_TOML"
      ok "Codex CLI"
    fi
  fi
else
  skip "Codex CLI"
fi

# --- VS Code (Copilot MCP) --------------------------------------------------
case "$OS" in
  Darwin) VSCODE_USER_DIR="$HOME/Library/Application Support/Code/User" ;;
  *)      VSCODE_USER_DIR="$HOME/.config/Code/User" ;;
esac
if [ -d "$VSCODE_USER_DIR" ]; then
  # VS Code's user-level MCP config uses a top-level "servers" key and a "type" field.
  if merge_json "$VSCODE_USER_DIR/mcp.json" "servers" "{\"type\": \"stdio\", \"command\": \"$CMD\", \"args\": $ARGS_JSON}"; then
    ok "VS Code"
  else
    fail "VS Code" "could not update $VSCODE_USER_DIR/mcp.json"
  fi
else
  skip "VS Code"
fi

echo ""
echo "==> Done."
[ ${#INSTALLED[@]} -gt 0 ] && echo "    configured: ${INSTALLED[*]}"
[ ${#SKIPPED[@]} -gt 0 ]   && echo "    not found:  ${SKIPPED[*]}"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "    FAILED:     ${FAILED[*]}" >&2
  echo "    (modified files were backed up to <file>.bak)" >&2
  exit 1
fi
echo "    Restart the affected apps (or start a new CLI session) to pick up the server."
