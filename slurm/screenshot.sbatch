#!/bin/bash
#SBATCH --job-name=swot-shot
#SBATCH --partition=batch
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G
#SBATCH --time=00:30:00
#SBATCH --output=logs/shot-%j.out
# Serve the explorer here and screenshot it in headless chromium (scenarios in scripts/screenshot.py): img/app-*.png
# Usage: sbatch slurm/screenshot.sbatch [EXTRA_URL PREFIX]   (also screenshots another page)
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export AWS_PROFILE=${AWS_PROFILE:-swot-read} AWS_REGION=${AWS_REGION:-us-east-1}
PIXI="/home/iury/.pixi/bin/pixi run"
PORT=$((5100 + RANDOM % 800))
$PIXI uvicorn server:app --app-dir app --port $PORT > logs/shot-server-$SLURM_JOB_ID.log 2>&1 &
for i in $(seq 120); do curl -sf -o /dev/null "http://localhost:$PORT/api/info" && break; sleep 5; done
[ $# -ge 2 ] && $PIXI python scripts/screenshot.py "$1" "$2"
rm -f img/app-*.png  # this run's only (img/history keeps every version): a scenario gone must not be copied again
$PIXI python scripts/screenshot.py "http://localhost:$PORT/" img/app open click series line hovmoller flat dateline phone
kill %1; grep -v "WARN cache" logs/shot-server-$SLURM_JOB_ID.log | tail -40
# keep every version: a dated copy (with the commit, + if there are uncommitted changes) in img/history
STAMP=$(date -u +%Y-%m-%dT%H%M)_$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- app || echo +)
for f in img/app-*.png; do cp "$f" "img/history/${STAMP}_$(basename $f)"; done
