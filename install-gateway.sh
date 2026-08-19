#!/usr/bin/env bash
# Quick-start installer for VectorStep Gateway.
#
#   curl -sSL https://raw.githubusercontent.com/bantex01/VectorStep-Gateway/main/install-gateway.sh | bash
#
# Safe to run more than once: existing config.yaml and agents/ are never
# overwritten.
set -euo pipefail

REPO_URL="https://github.com/bantex01/VectorStep-Gateway.git"
INSTALL_DIR="$HOME/.vectorstep-gateway"

log()  { printf '==> %s\n' "$1"; }
skip() { printf '==> skip: %s\n' "$1"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

# --- Preflight ---------------------------------------------------------

command -v git >/dev/null 2>&1 || die "git not found on PATH. Install git and re-run."

PYTHON_BIN=""
for candidate in python3.11 python3.12 python3.13 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
[ -n "$PYTHON_BIN" ] || die "no python3 found on PATH. Install Python 3.11+ and re-run."

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
  || die "found $PYTHON_BIN ($PYTHON_VERSION), but VectorStep Gateway needs Python 3.11+."

"$PYTHON_BIN" -m pip --version >/dev/null 2>&1 || die "pip not available for $PYTHON_BIN. Install pip and re-run."

log "preflight ok (git, $PYTHON_BIN $PYTHON_VERSION, pip)"

# --- Clone ---------------------------------------------------------------

if [ -d "$INSTALL_DIR/.git" ]; then
  skip "$INSTALL_DIR already a git checkout, not re-cloning"
else
  log "cloning into $INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- Virtualenv + deps -----------------------------------------------------

if [ -d ".venv" ]; then
  skip ".venv already exists, not recreating"
else
  log "creating virtualenv"
  "$PYTHON_BIN" -m venv .venv
fi

log "installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# --- Config ----------------------------------------------------------------

if [ -f "config.yaml" ]; then
  skip "config.yaml already exists, leaving it alone"
else
  log "writing config.yaml from samples/config.yaml.example"
  cp samples/config.yaml.example config.yaml
fi

if [ -d "agents" ]; then
  skip "agents/ already exists"
else
  log "creating empty agents/"
  mkdir -p agents
fi

# --- Summary -----------------------------------------------------------

cat <<EOF

Gateway installed at $INSTALL_DIR

Next steps:

  1. Edit $INSTALL_DIR/config.yaml with your LLM provider keys and any MCP servers.
  2. Add a first agent under $INSTALL_DIR/agents/ (see the tutorials at
     https://vectorstep.io/docs/tutorials/build-your-first-agent/) — or leave
     agents/ empty for now, an agent-less Gateway is a valid starting state.
  3. Set your LLM provider key, e.g.:

       export ANTHROPIC_API_KEY=sk-ant-...

  4. Start the Gateway:

       cd $INSTALL_DIR && source .venv/bin/activate && python -m gateway.main

  5. On first run, find the operator token VectorStep needs:

       cat $INSTALL_DIR/identity/device-auth.json

EOF
