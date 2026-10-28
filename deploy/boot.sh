#!/bin/bash
# Runs on the EC2 instance at every boot, from the package slurm/ec2.sh sends (unpacked in /opt/app): build the
# image, serve it behind Caddy (HTTPS at <ip>.sslip.io), and stop the machine when nobody used it for IDLE minutes.
# A stopped instance costs only its disk and address; deploy/wake.py starts it again on the next visit.
IDLE=30
cd /opt/app
t=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
ip=$(curl -s -H "X-aws-ec2-metadata-token: $t" http://169.254.169.254/latest/meta-data/public-ipv4)
docker build -t swot .
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
mkdir -p /opt/cache
docker run -d --name swot --restart always -p 127.0.0.1:7860:7860 -e SWOT_NETCDF=0 -e SWOT_CACHE_GB=6 \
  -e SWOT_DETAIL_BLOCKS=512 -e SWOT_BASE_BLOCKS=2048 -v /opt/cache:/cache -e SWOT_CACHE_DIR=/cache "${store[@]}" swot
set -x
docker run -d --name caddy --restart always --network host -v caddy:/data caddy:2 \
  caddy reverse-proxy --from "${ip//./-}.sslip.io" --to localhost:7860
# every request shows up in the server's access log; none in the last IDLE minutes (and up for as long): stop
systemctl stop swot-idle 2>/dev/null
systemd-run --unit swot-idle bash -c "while sleep 60; do
  [ \$(cut -d. -f1 /proc/uptime) -gt $((IDLE * 60)) ] || continue
  docker logs --since ${IDLE}m swot 2>&1 | grep -q '\"GET ' || shutdown -h now
done"
