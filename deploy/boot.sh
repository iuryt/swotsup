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
# the store: Arraylake (Earthmover) if its token is in SSM (/swot-explorer/arraylake-token), else the S3 copy;
# no command echo (bash -x) while the token is around: the boot log is readable from the EC2 console
set +x
token=$(aws ssm get-parameter --region us-east-1 --name /swot-explorer/arraylake-token --with-decryption \
  --query Parameter.Value --output text 2>/tmp/ssm.err)
[ -n "$token" ] || echo "no Arraylake token: $(tail -1 /tmp/ssm.err)"  # AWS's message, without the token
store=()
[ -n "$token" ] && store=(-e "ARRAYLAKE_TOKEN=$token" -e SWOT_STORE=arraylake:computational-oceanography-udel/swot-l3-lr-ssh-basic-v3)
echo "store: ${store[3]:-the S3 copy}"
docker run -d --name swot --restart always -p 127.0.0.1:7860:7860 -e SWOT_NETCDF=0 -e SWOT_CACHE_GB=8 "${store[@]}" swot
set -x
docker run -d --name caddy --restart always --network host -v caddy:/data caddy:2 \
  caddy reverse-proxy --from "${ip//./-}.sslip.io" --to localhost:7860
# every request shows up in the server's access log; none in the last IDLE minutes (and up for as long): stop
systemctl stop swot-idle 2>/dev/null
systemd-run --unit swot-idle bash -c "while sleep 60; do
  [ \$(cut -d. -f1 /proc/uptime) -gt $((IDLE * 60)) ] || continue
  docker logs --since ${IDLE}m swot 2>&1 | grep -q '\"GET ' || shutdown -h now
done"
