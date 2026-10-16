#!/bin/bash
#SBATCH -p batch -c 8 --mem 24G -t 120 -o logs/versions-%j.out
# Screenshots of earlier versions into img/history, named <commit date UTC>_<hash>_app-<scenario>.png: each
# commit is checked out in its own worktree and served with today's environment, and gets the scenarios its
# page has (open, click, series; line if it has the line tool; flat if it has the globe).
# Usage: sbatch slurm/screenshot_versions.sbatch HASH...
set -uo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export AWS_PROFILE=${AWS_PROFILE:-swot-read} AWS_REGION=${AWS_REGION:-us-east-1}
PIXI="/home/iury/.pixi/bin/pixi run --manifest-path $PWD/pyproject.toml"
TREES=$(mktemp -d -p "$PWD/logs" versions-XXXX)
for hash in "$@"; do
  tree=$TREES/$hash
  git worktree add -q --detach "$tree" "$hash"
  scenarios="open click series"
  grep -q 'id="line"' "$tree/app/static/index.html" && scenarios="$scenarios line"
  grep -q 'id="projection"' "$tree/app/static/index.html" && scenarios="$scenarios flat"
  PORT=$((5100 + RANDOM % 800))
  $PIXI uvicorn server:app --app-dir "$tree/app" --port $PORT > "$TREES/$hash.log" 2>&1 &
  for i in $(seq 120); do curl -sf -o /dev/null "http://localhost:$PORT/api/info" && break; sleep 5; done
  stamp=$(TZ=UTC git log -1 --format=%cd --date=format-local:%Y-%m-%dT%H%M "$hash")_$(git rev-parse --short "$hash")
  echo "== $stamp: $scenarios"
  $PIXI python scripts/screenshot.py "http://localhost:$PORT/" "img/history/${stamp}_app" $scenarios 2>&1 | grep -v WARN
  kill %1; wait %1 2>/dev/null
  git worktree remove --force "$tree"
done
rm -rf "$TREES"
git worktree prune
