#!/bin/bash
#SBATCH --job-name=pixi-install
#SBATCH --partition=batch
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=logs/pixi-%j.out
# Install the environment and the headless chromium used by scripts/screenshot.py.
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export PIXI_CACHE_DIR="${TMPDIR:-/tmp}/pixi-cache-$USER"
/home/iury/.pixi/bin/pixi install
/home/iury/.pixi/bin/pixi run python -m playwright install chromium
/home/iury/.pixi/bin/pixi list | grep -E "^(icechunk|xarray|datashader|fastapi) "
