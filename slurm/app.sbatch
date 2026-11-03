#!/bin/bash
#SBATCH --job-name=swot-app
#SBATCH --partition=batch
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G
#SBATCH --time=08:00:00
#SBATCH --output=logs/app-%j.out
# Serve the viewer on a compute node; open it through an ssh tunnel (command printed in the log).
# Reloads when app/ changes. Usage: sbatch slurm/app.sbatch   (L2 Expert + Unsmoothed by default; SWOT_STORE + SWOT_PRODUCT=l3 for L3)
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export AWS_PROFILE=${AWS_PROFILE:-swot-read} AWS_REGION=${AWS_REGION:-us-east-1}
PORT=${PORT:-5006}
# edits come from the login node over NFS, which sends this node no file events: look for changes instead
export WATCHFILES_FORCE_POLLING=true
echo "host=$(hostname) start=$(date -Is)"
echo "on your machine: ssh -N -L $PORT:$(hostname):$PORT biomix   then open http://localhost:$PORT"
pixi run uvicorn server:app --app-dir app --host 0.0.0.0 --port $PORT \
    --reload --reload-dir app 2>&1 | grep --line-buffered -v "WARN cache"
