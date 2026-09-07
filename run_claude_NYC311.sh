#!/bin/bash
# run_claude_NYC311.sh
# Launches Claude Code for the NYC311 project with the Socrata app token loaded.
#
# The token is scoped to this session only: it is exported into claude and the
# agents it spawns, and dies with the session. It is deliberately not in
# ~/.bashrc, which would leak it into every shell on the machine.
#
# Usage:  ./run_claude_NYC311.sh [claude args...]

set -euo pipefail

# The project is wherever this script lives, so the path is never hardcoded.
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$HOME/.nyc311.env"

cd "$PROJECT_DIR" || { echo "ERROR: cannot enter $PROJECT_DIR" >&2; exit 1; }

# Socrata app token for the 311 update API. The file is mode 600 and lives in
# $HOME, outside this Dropbox-synced folder — only its path appears here.
if [[ -f "$ENV_FILE" ]]; then
  mode="$(stat -c '%a' "$ENV_FILE")"
  if [[ "$mode" != "600" ]]; then
    echo "WARNING: $ENV_FILE is mode $mode, expected 600 — run: chmod 600 $ENV_FILE" >&2
  fi
  # shellcheck source=/dev/null
  . "$ENV_FILE"
else
  echo "WARNING: no $ENV_FILE — ETL will fall back to the throttled shared pool" >&2
fi

command -v claude >/dev/null || { echo "ERROR: claude is not on PATH" >&2; exit 127; }

exec claude "$@"
