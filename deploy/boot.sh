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
docker run -d --name swot --restart always -p 127.0.0.1:7860:7860 -e SWOT_NETCDF=0 -e SWOT_CACHE_GB=8 swot
docker run -d --name caddy --restart always --network host -v caddy:/data caddy:2 \
  caddy reverse-proxy --from "${ip//./-}.sslip.io" --to localhost:7860
# every request shows up in the server's access log; none in the last IDLE minutes (and up for as long): stop
systemctl stop swot-idle 2>/dev/null
systemd-run --unit swot-idle bash -c "while sleep 60; do
  [ \$(cut -d. -f1 /proc/uptime) -gt $((IDLE * 60)) ] || continue
  docker logs --since ${IDLE}m swot 2>&1 | grep -q '\"GET ' || shutdown -h now
done"
