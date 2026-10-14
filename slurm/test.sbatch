#!/bin/bash
#SBATCH --job-name=swot-test
#SBATCH --partition=batch
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --output=logs/test-%j.out
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-$PWD}"
/home/iury/.pixi/bin/pixi run test "$@"
