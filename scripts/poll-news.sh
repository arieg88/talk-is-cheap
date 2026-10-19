#!/usr/bin/env bash
# One news poll, for a scheduler: Windows Task Scheduler through wsl.exe, or cron.
# Appends a timestamp and the poll's output to .cache/news-poll.log in the project.
# A run that starts while the previous one is still going logs one line and exits.
set -euo pipefail

# The project is the parent of this script's directory, wherever it is checked out.
project="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$project"
mkdir -p .cache
log=.cache/news-poll.log

# wsl.exe and cron start with a minimal PATH; uv's installer puts uv in ~/.local/bin.
# Set UV to use another location.
uv="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

# The command itself also takes a database lock, so overlapping runs never interleave
# writes; this only avoids starting uv for nothing.
exec 9>.cache/news-poll.lock
if ! flock -n 9; then
    echo "--- $(date -Is) previous poll still running; skipped" >> "$log"
    exit 0
fi

echo "--- $(date -Is)" >> "$log"
"$uv" run talk-is-cheap ingest news >> "$log" 2>&1
