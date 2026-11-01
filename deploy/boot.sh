#!/bin/bash
# Runs on the EC2 instance at every boot, from the package slurm/ec2.sh sends (unpacked in /opt/app): build the
# image, serve it behind Caddy (HTTPS at <ip>.sslip.io), and stop the machine when nobody used it for IDLE minutes.
# A stopped instance costs only its disk and address; deploy/wake.py starts it again on the next visit.
IDLE=30
# the disk may have grown (slurm/ec2.sh update): its partition and file system to its size (nothing to do otherwise)
growpart /dev/nvme0n1 1 >/dev/null 2>&1; xfs_growfs / >/dev/null 2>&1
cd /opt/app
stage=$(cat stage 2>/dev/null || echo prod)  # dev: the instance for testing branches (slurm/ec2.sh)
telemetry=s3://iuryt-shared-east/telemetry/swot-explorer$([ "$stage" = dev ] && echo -dev)/
t=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
ip=$(curl -s -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/public-ipv4)
# the disk (30 GB) keeps every image built: the build cache goes before building, the unused images once the new
# container runs (a full disk failed an update, and the instance kept the old image)
docker builder prune -af >/dev/null 2>&1
docker build -t swot .
# the heavy requests' Lambda function (slurm/ec2.sh work) runs this same image: pushed as this stage's, and the
# function moved to it, so it always has the server's code; the server asks it for series (and works them out
# itself if it cannot)
account=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
registry=$account.dkr.ecr.us-east-1.amazonaws.com work=swot-explorer-work$([ "$stage" = dev ] && echo -dev)
if aws ecr get-login-password --region us-east-1 2>/dev/null | docker login -u AWS --password-stdin $registry >/dev/null 2>&1 \
  && docker tag swot $registry/swot-explorer-work:$stage && docker push -q $registry/swot-explorer-work:$stage; then
  aws lambda update-function-code --region us-east-1 --function-name $work --image-uri $registry/swot-explorer-work:$stage \
    --query LastUpdateStatus --output text 2>&1 | tail -1
fi
docker rm -f swot caddy
# the stores: the server's default, L2 Expert with L2 Unsmoothed from a zoom on, on Arraylake (Earthmover), if its
# token is in SSM (/swot-explorer/arraylake-token); else the L3 copy on S3. No command echo (bash -x) while the token
# is around: the boot log is readable from the EC2 console. Memory (16 GB): views up to 6 GB, 512 blocks of 250 m
# (~1.7 GB) read ahead or for tiles, 2048 of Expert (~0.8 GB) for the opening cycle up close. The opening cycle's
# overview is kept on the instance's disk (/opt/cache): a restart reads it back instead of the whole cycle
set +x
token=$(aws ssm get-parameter --region us-east-1 --name /swot-explorer/arraylake-token --with-decryption \
  --query Parameter.Value --output text 2>/tmp/ssm.err)
[ -n "$token" ] || echo "no Arraylake token: $(tail -1 /tmp/ssm.err)"  # AWS's message, without the token
store=(-e SWOT_STORE=s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2 -e SWOT_PRODUCT=l3)
[ -n "$token" ] && store=(-e "ARRAYLAKE_TOKEN=$token")
echo "store: $([ -n "$token" ] && echo "L2 on Arraylake" || echo "the L3 copy on S3")"
install -d -o 1000 -g 1000 /opt/cache /opt/telemetry /opt/telemetry/debug  # the server runs as user 1000 (Dockerfile)
chown -R 1000:1000 /opt/cache /opt/telemetry
# the daily maps (app/rasters.py: the whole globe on any dates at once), swot_cloud's repository next to Expert's
rasters=()
[ -n "$token" ] && rasters=(-e SWOT_RASTERS=arraylake:computational-oceanography-udel/swot-l2-lr-ssh-expert-daily-maps)
rm -rf /opt/rasters  # the copy of the earlier rasters
# dev: pages opened with ?debug=1 report the map's events (copied to S3 every minute with the usage)
debug=()
[ "$stage" = dev ] && debug=(-e SWOT_DEBUG_DIR=/telemetry/debug -e SWOT_TILE_SAMPLE=1)  # and every tile's time
# the country of a visitor (anonymous usage): DB-IP Lite, this month's (else last month's), kept with the cache
geo=/opt/cache/dbip-country-lite.mmdb
for month in $(date +%Y-%m) $(date -d "-1 month" +%Y-%m); do
  [ -s $geo ] && [ $(( $(date +%s) - $(stat -c %Y $geo) )) -lt 2678400 ] && break
  curl -sfL "https://download.db-ip.com/free/dbip-country-lite-$month.mmdb.gz" | gunzip > $geo.new && mv $geo.new $geo && break
done
chown 1000:1000 $geo 2>/dev/null
# the page's basemap (GEBCO_2026 relief, one 0.6 GB file: scripts/render_basemap.py), kept with the cache; fetched once
basemap=/opt/cache/gebco2026.mbtiles
[ -s $basemap ] || aws s3 cp --only-show-errors s3://iuryt-shared-east/deploy/swot_explorer/gebco2026.mbtiles $basemap.new \
  && mv $basemap.new $basemap
chown 1000:1000 $basemap 2>/dev/null
# downloads: of L2 (NASA/JPL, CNES), not of the L3 fallback (its license does not allow redistributing it unchanged)
netcdf=$([ -n "$token" ] && echo 1 || echo 0)
docker run -d --name swot --restart always -p 127.0.0.1:7860:7860 -e SWOT_NETCDF=$netcdf -e SWOT_CACHE_GB=6 \
  -e SWOT_DETAIL_BLOCKS=512 -e SWOT_BASE_BLOCKS=2048 -v /opt/cache:/cache -e SWOT_CACHE_DIR=/cache \
  -e SWOT_PUBLIC_URL="$(cat public_url 2>/dev/null)" -v /opt/telemetry:/telemetry -e SWOT_TELEMETRY_DIR=/telemetry \
  -e SWOT_GEOIP=/cache/dbip-country-lite.mmdb -e SWOT_BASEMAP=/cache/gebco2026.mbtiles "${rasters[@]}" "${debug[@]}" "${store[@]}" \
  -e SWOT_LAMBDA=$work -e SWOT_THREADS=100 --env-file <(grep -s = server.env) swot  # server.env: settings being tried (slurm/ec2.sh SERVER_ENV)
set -x
docker run -d --name caddy --restart always --network host -v caddy:/data caddy:2 \
  caddy reverse-proxy --from "${ip//./-}.sslip.io" --to localhost:7860
docker image prune -af >/dev/null 2>&1
df -h / | tail -1
# the anonymous usage, copied to S3 every 10 minutes and before the machine stops (it may only write there)
sync="aws s3 sync --only-show-errors /opt/telemetry $telemetry"
systemctl stop swot-telemetry.timer 2>/dev/null
systemd-run --unit swot-telemetry --on-calendar="$([ "$stage" = dev ] && echo '*:*:00' || echo '*:0/10')" bash -c "$sync"
# every request shows up in the server's access log; none in the last IDLE minutes (and up for as long): stop
systemctl stop swot-idle 2>/dev/null
systemd-run --unit swot-idle bash -c "while sleep 60; do
  [ \$(cut -d. -f1 /proc/uptime) -gt $((IDLE * 60)) ] || continue
  docker logs --since ${IDLE}m swot 2>&1 | grep -q '\"GET ' || { $sync; shutdown -h now; }
done"
