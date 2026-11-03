#!/bin/bash
#SBATCH --job-name=swot
#SBATCH --partition=batch
#SBATCH --cpus-per-task=16
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=logs/run-%j.out
# Run a script in this project's env: sbatch slurm/run.sbatch scripts/foo.py ARGS...
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export PIXI_CACHE_DIR="${TMPDIR:-/tmp}/pixi-cache-$USER"
echo "host=$(hostname) start=$(date -Is) $*"
pixi run python "$@" 2>&1 | grep --line-buffered -v WARN
echo "done=$(date -Is)"
