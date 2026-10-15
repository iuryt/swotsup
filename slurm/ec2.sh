#!/bin/bash
# The explorer on one EC2 instance in us-east-1 (next to the store), behind Caddy with HTTPS at
# https://<ip>.sslip.io. The instance reads the store and the app (packed into the bucket) through its own
# read-only role, so no key goes to the machine; there is no SSH, an update reboots it and it rebuilds.
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh up       create the role, the security group and the instance
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh update   send app/ again and reboot
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh status   address and the end of the boot log
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh down     terminate the instance and delete what `up` created
# DEPLOY_PROFILE (EC2, the swot-explorer role, deploy/swot_explorer/ in the bucket) and UPLOAD_PROFILE name AWS profiles.
#SBATCH -p batch -c 1 --mem 2G -t 30
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-.}"

NAME=swot-explorer TYPE=${TYPE:-c7i.2xlarge} BUCKET=iuryt-shared-east
STORE=icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2 CODE=deploy/swot_explorer/app.tar.gz
export AWS_REGION=us-east-1
aws() { pixi exec --spec awscli aws "$@"; }
deploy() { aws --profile "${DEPLOY_PROFILE:-swot-deploy}" "$@"; }
instance() {
  deploy ec2 describe-instances --query 'Reservations[].Instances[].InstanceId' --output text \
    --filters Name=tag:Name,Values=$NAME Name=instance-state-name,Values=pending,running,stopping,stopped
}
send_code() {
  tar -czf logs/app.tar.gz --exclude __pycache__ -C app .
  aws --profile "${UPLOAD_PROFILE:-swot-deploy}" s3 cp logs/app.tar.gz s3://$BUCKET/$CODE
}

case $1 in
up)
  [ -z "$(instance)" ] || { echo "already running: $(instance)"; exit 1; }
  send_code
  deploy iam create-role --role-name $NAME --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  deploy iam put-role-policy --role-name $NAME --policy-name read --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$BUCKET/$STORE/*\",\"arn:aws:s3:::$BUCKET/$CODE\"]},
    {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\",
     \"Condition\":{\"StringLike\":{\"s3:prefix\":\"$STORE/*\"}}}]}"
  deploy iam create-instance-profile --instance-profile-name $NAME
  deploy iam add-role-to-instance-profile --instance-profile-name $NAME --role-name $NAME
  sg=$(deploy ec2 create-security-group --group-name $NAME --description "SWOT explorer, web only" --query GroupId --output text)
  for port in 80 443; do
    deploy ec2 authorize-security-group-ingress --group-id "$sg" --protocol tcp --port $port --cidr 0.0.0.0/0
  done
  ami=$(deploy ec2 describe-images --owners amazon --filters 'Name=name,Values=al2023-ami-2023*-x86_64' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
  # cloud-init installs docker and Caddy's image once, then this runs on every boot: fetch the app, build, serve
  cat > logs/ec2-user-data.sh <<EOF
#!/bin/bash
dnf install -y docker && systemctl enable --now docker
cat > /var/lib/cloud/scripts/per-boot/swot.sh <<'BOOT'
#!/bin/bash
set -x
t=\$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
ip=\$(curl -s -H "X-aws-ec2-metadata-token: \$t" http://169.254.169.254/latest/meta-data/public-ipv4)
rm -rf /opt/app && mkdir -p /opt/app && aws s3 cp s3://$BUCKET/$CODE - | tar -xz -C /opt/app
docker build -t swot /opt/app
docker rm -f swot caddy
docker run -d --name swot --restart always -p 127.0.0.1:7860:7860 -e SWOT_NETCDF=0 -e SWOT_CACHE_GB=8 swot
docker run -d --name caddy --restart always --network host -v caddy:/data caddy:2 \\
  caddy reverse-proxy --from \${ip//./-}.sslip.io --to localhost:7860
BOOT
chmod +x /var/lib/cloud/scripts/per-boot/swot.sh
/var/lib/cloud/scripts/per-boot/swot.sh
EOF
  sleep 10  # a new instance profile takes a few seconds to be usable
  deploy ec2 run-instances --image-id "$ami" --instance-type "$TYPE" --security-group-ids "$sg" \
    --iam-instance-profile Name=$NAME --user-data file://logs/ec2-user-data.sh \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3}' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" --query 'Instances[0].InstanceId' --output text
  deploy ec2 wait instance-running --instance-ids "$(instance)"
  ;&
status)
  id=$(instance)
  ip=$(deploy ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  echo "instance $id: https://${ip//./-}.sslip.io (a few minutes after the start for the build and the opening view)"
  deploy ec2 get-console-output --instance-id "$id" --latest --output text | tail -40 || true
  ;;
update)
  send_code
  deploy ec2 reboot-instances --instance-ids "$(instance)"
  echo "rebooting $(instance): it rebuilds and is back in a few minutes at the same address"
  ;;
down)
  id=$(instance)
  if [ -n "$id" ]; then deploy ec2 terminate-instances --instance-ids $id; deploy ec2 wait instance-terminated --instance-ids $id; fi
  deploy ec2 delete-security-group --group-name $NAME || true
  deploy iam remove-role-from-instance-profile --instance-profile-name $NAME --role-name $NAME || true
  deploy iam delete-instance-profile --instance-profile-name $NAME || true
  deploy iam delete-role-policy --role-name $NAME --policy-name read || true
  deploy iam delete-role --role-name $NAME || true
  aws --profile "${UPLOAD_PROFILE:-swot-deploy}" s3 rm s3://$BUCKET/$CODE || true
  ;;
esac
