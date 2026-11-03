#!/bin/bash
#SBATCH -c 4 --mem 8G -t 180 -o logs/load-trial-%j.out
# One load trial on the dev server: restart it with settings to try, replay the recorded visits at some levels
# (scripts/load_test.py), then the server's own numbers from its telemetry (health and request times) during the trial.
# Usage: SERVER_ENV="SWOT_LOADS=4 WEB_CONCURRENCY=2" TYPE=... sbatch slurm/load_trial.sh LEVELS [MINUTES] [STORIES]
set -u
cd "${SLURM_SUBMIT_DIR:-$PWD}"
LEVELS=${1:-20} MINUTES=${2:-4} STORIES=${3:-logs/load-stories.json}
URL=https://54-85-79-178.sslip.io/
export STAGE=dev SERVER_ENV="SWOT_HEALTH_SECONDS=10 ${SERVER_ENV:-}"
echo "settings: $SERVER_ENV; code $(git rev-parse --short HEAD)$(git diff --quiet HEAD -- app deploy || echo +)"
bash slurm/ec2.sh update | tail -1
# the old server may answer for a while: wait for it to go down (the reboot), then for the new one
for i in $(seq 40); do curl -sf -m 5 -o /dev/null ${URL}api/info || break; sleep 5; done
for i in $(seq 100); do sleep 15; curl -sf -m 10 -o /dev/null ${URL}api/info && break; done
sleep 30  # the opening overview read back from disk
# one series first: the Lambda function (if any) started with the new image, as after a deploy
curl -s -m 200 -o /dev/null -w "first series: %{time_total} s\n" "${URL}api/series?lon=-30&lat=40&variable=ssha_dtu25_cal"
t0=$(date -u +%Y-%m-%dT%H:%M:%S)
pixi run python scripts/load_test.py replay $URL $STORIES $LEVELS $MINUTES
t1=$(date -u +%Y-%m-%dT%H:%M:%S)
sleep 90  # dev copies its telemetry to S3 every minute
day=${t0:0:10} dir=logs/trial-$SLURM_JOB_ID
mkdir -p $dir
for f in health requests; do
  pixi exec --spec awscli aws --profile swot-read s3 cp --only-show-errors \
    s3://iuryt-shared-east/telemetry/swot-explorer-dev/$f-$day.jsonl $dir/$f.jsonl
done
pixi run python - "$dir" "$t0" "$t1" <<'EOF'
import sys
import pandas as pd
d, t0, t1 = sys.argv[1], pd.Timestamp(sys.argv[2], tz="UTC"), pd.Timestamp(sys.argv[3], tz="UTC")
pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_rows", 200)
h = pd.read_json(f"{d}/health.jsonl", lines=True)
h["time"] = pd.to_datetime(h.time)
h = h[(h.time >= t0) & (h.time <= t1)]
print("\n== the server's health during the trial (every 10 s; per process)")
print(h.drop(columns=["time"]).describe(percentiles=[.5, .9]).T[["mean", "50%", "90%", "max"]].round(2))
r = pd.read_json(f"{d}/requests.jsonl", lines=True)
r["time"] = pd.to_datetime(r.time)
r = r[(r.time >= t0) & (r.time <= t1)]
print("\n== server-side seconds by endpoint")
print(r.groupby("endpoint").seconds.describe(percentiles=[.5, .95])[["count", "50%", "95%", "max"]].round(2))
EOF
