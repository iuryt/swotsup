#!/bin/bash
#SBATCH -p batch -c 16 --mem 24G -t 4:00:00 -o logs/rasters-%A_%a.out
# Daily rasters (app/rasters.py): job array, each task builds DAYS days from START + its index x DAYS.
#   sbatch slurm/run.sbatch app/rasters.py OUT create              # once: the arrays
#   sbatch --array=0-6 slurm/rasters.sbatch OUT 2026-10-01 3     # 21 days, 7 tasks at once
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
OUT=$1 START=$2 DAYS=$3
FIRST=$(date -u -d "$START + $(( ${SLURM_ARRAY_TASK_ID:-0} * DAYS )) days" +%F)
# the environment's python itself: tasks starting `pixi run` at once collide
"$PWD/.pixi/envs/default/bin/python" app/rasters.py "$OUT" "$FIRST" "$DAYS"
