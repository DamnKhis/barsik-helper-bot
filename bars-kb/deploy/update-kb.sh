#!/usr/bin/env bash
# Refresh the handoff package (`dist/`) when the documentation changes.
#
# Intended for a server: run it from cron or a systemd timer. Two sources of
# documents are supported, and both are safe to combine:
#
#   1. Documents live in this repository (docs/source). `sync` compares the
#      content digest and rebuilds only when something actually changed.
#   2. Documents are maintained on GitHub. Actions rebuilds `dist/` and commits
#      it; `--git-pull` brings the new package and `sync` then skips the build
#      because the digest already matches.
#
# If CI built the package with an embedded Qdrant, the collection does not exist
# on the server yet: pass `--restore` to load the build from `dist/LATEST` into
# the running Qdrant.
#
# Usage:
#   deploy/update-kb.sh [--git-pull] [--restore] [extra bars-kb sync args]
set -euo pipefail

KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KB_DIR"

PULL=0
RESTORE=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --git-pull) PULL=1 ;;
    --restore) RESTORE=1 ;;
    *) ARGS+=("$arg") ;;
  esac
done

PY="$KB_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Always run the pipeline from this checkout: a globally installed bars-kb may
# belong to another project and would use a different data/ and dist/.
export PYTHONPATH="$KB_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
if ! "$PY" -c "import bars_kb.cli" >/dev/null 2>&1; then
  log "bars-kb is not importable with $PY; run 'make install' in $KB_DIR"
  exit 2
fi

if [ "$PULL" = "1" ]; then
  if git -C "$KB_DIR" pull --ff-only --quiet; then
    log "git pull: up to date"
  else
    log "git pull failed; continuing with the local checkout"
  fi
fi

log "sync: start"
"$PY" -m bars_kb.cli sync "${ARGS[@]}"

if [ "$RESTORE" = "1" ]; then
  if [ -z "${QDRANT_URL:-}" ]; then
    log "--restore needs QDRANT_URL"
    exit 2
  fi
  log "restore: $(cat "$KB_DIR/dist/LATEST") -> $QDRANT_URL"
  PYTHONPATH="$KB_DIR/examples" "$PY" "$KB_DIR/examples/restore_from_export.py" \
    --export "$KB_DIR/dist" --qdrant-url "$QDRANT_URL"
fi

log "done: build $(cat "$KB_DIR/dist/LATEST")"
