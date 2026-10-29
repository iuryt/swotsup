#!/bin/bash
# The explorer on one EC2 instance in us-east-1 (next to the store), behind Caddy with HTTPS at
# https://<elastic ip>.sslip.io. It stops itself after 30 idle minutes (deploy/boot.sh); the link to share is a
# Lambda function URL (deploy/wake.py) that starts it again and shows a waiting page meanwhile (~1 min).
# The instance reads the store and the app (packed into the bucket) through its own read-only role, so no key
# goes to the machine; the image installs the pixi environment `serve` (pixi.lock); there is no SSH.
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh up       create roles, address, instance and link
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh update   send app/ and deploy/ again; reboot if running (else at its next start)
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh status   link, state and the end of the boot log
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh down     delete everything `up` created
#   sbatch -o logs/ec2-%j.out slurm/ec2.sh domain   https://$DOMAIN in front of it all (after `up`; then `update`)
# The domain: CloudFront with an ACM certificate and Route 53 records in the domain's zone (registered in Route 53).
# Its origin is the instance, failing over to the Lambda function (502/503, or no connection: it sleeps), whose
# waiting page then stays at the same address; `down` leaves the domain's pieces in place.
# DEPLOY_PROFILE: EC2, the roles swot-explorer and swot-explorer-wake, the Lambda function swot-explorer-wake and
# deploy/swot_explorer/ in the bucket.
#SBATCH -p batch -c 1 --mem 2G -t 30
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-.}"

NAME=swot-explorer WAKE=swot-explorer-wake TYPE=${TYPE:-c7i.2xlarge} BUCKET=iuryt-shared-east
STORE=icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2 CODE=deploy/swot_explorer/app.tar.gz
TELEMETRY=telemetry/swot-explorer  # where the instance writes the anonymous usage (write only)
DOMAIN=${DOMAIN:-swotsup.org}
export AWS_REGION=us-east-1
aws() { pixi exec --spec awscli aws --profile "${DEPLOY_PROFILE:-swot-deploy}" "$@"; }
instance() {
  aws ec2 describe-instances --query 'Reservations[].Instances[].InstanceId' --output text \
    --filters Name=tag:Name,Values=$NAME Name=instance-state-name,Values=pending,running,stopping,stopped
}
address() { aws ec2 describe-addresses --filters Name=tag:Name,Values=$NAME --query "Addresses[0].$1" --output text; }
distribution() { aws cloudfront list-distributions --query "DistributionList.Items[?Aliases.Items[0]=='$DOMAIN'].$1 | [0]" --output text; }
send_code() {  # build context (Dockerfile at the root), the boot script, and the link to share (copied links start with it)
  if [ "$(distribution Id)" != None ]; then echo "https://$DOMAIN/" > logs/public_url
  else aws lambda get-function-url-config --function-name $WAKE --query FunctionUrl --output text > logs/public_url 2>/dev/null || : > logs/public_url; fi
  tar -czf logs/app.tar.gz --exclude __pycache__ Dockerfile pyproject.toml pixi.lock app -C deploy boot.sh -C ../logs public_url
  aws s3 cp logs/app.tar.gz s3://$BUCKET/$CODE
}
# what the instance may read: the S3 store, the app package, and the secrets under /swot-explorer/ in SSM
# (the Arraylake token, put there by the user in the console); and write: the anonymous usage, nothing else
INSTANCE_POLICY="
    {\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$BUCKET/$STORE/*\",\"arn:aws:s3:::$BUCKET/$CODE\"]},
    {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\",
     \"Condition\":{\"StringLike\":{\"s3:prefix\":\"$STORE/*\"}}},
    {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:us-east-1:*:parameter/swot-explorer/*\"},
    {\"Effect\":\"Allow\",\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/$TELEMETRY/*\"},
    {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\",
     \"Condition\":{\"StringLike\":{\"s3:prefix\":\"$TELEMETRY/*\"}}}"
role() {  # name, service that assumes it, inline policy
  aws iam create-role --role-name "$1" --assume-role-policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}"
  aws iam put-role-policy --role-name "$1" --policy-name main --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$3]}"
}

case $1 in
up)
  [ -z "$(instance)" ] || { echo "already there: $(instance)"; exit 1; }
  send_code
  role $NAME ec2.amazonaws.com "$INSTANCE_POLICY"
  aws iam create-instance-profile --instance-profile-name $NAME
  aws iam add-role-to-instance-profile --instance-profile-name $NAME --role-name $NAME
  sg=$(aws ec2 create-security-group --group-name $NAME --description "SWOT explorer, web only" --query GroupId --output text)
  for port in 80 443; do
    aws ec2 authorize-security-group-ingress --group-id "$sg" --protocol tcp --port $port --cidr 0.0.0.0/0
  done
  aws ec2 allocate-address --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]"
  ami=$(aws ec2 describe-images --owners amazon --filters 'Name=name,Values=al2023-ami-2023*-x86_64' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
  # cloud-init installs docker once; at every boot: fetch the package and run its boot.sh
  cat > logs/ec2-user-data.sh <<EOF
#!/bin/bash
dnf install -y docker && systemctl enable --now docker
cat > /var/lib/cloud/scripts/per-boot/swot.sh <<'BOOT'
#!/bin/bash
rm -rf /opt/app && mkdir -p /opt/app && aws s3 cp s3://$BUCKET/$CODE - | tar -xz -C /opt/app
bash -x /opt/app/boot.sh
BOOT
chmod +x /var/lib/cloud/scripts/per-boot/swot.sh
/var/lib/cloud/scripts/per-boot/swot.sh  # cloud-init runs per-boot scripts before user data: the first boot by hand
EOF
  sleep 10  # a new instance profile takes a few seconds to be usable
  aws ec2 run-instances --image-id "$ami" --instance-type "$TYPE" --security-group-ids "$sg" \
    --iam-instance-profile Name=$NAME --user-data file://logs/ec2-user-data.sh \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3}' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" --query 'Instances[0].InstanceId' --output text
  aws ec2 wait instance-running --instance-ids "$(instance)"
  # the fixed address, there long before boot.sh asks for it (installing docker comes first)
  aws ec2 associate-address --instance-id "$(instance)" --allocation-id "$(address AllocationId)"
  role $WAKE lambda.amazonaws.com "
    {\"Effect\":\"Allow\",\"Action\":\"ec2:DescribeInstances\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":\"ec2:StartInstances\",\"Resource\":\"*\",
     \"Condition\":{\"StringEquals\":{\"aws:ResourceTag/Name\":\"$NAME\"}}},
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}"
  (cd deploy && pixi exec --spec python python -m zipfile -c ../logs/wake.zip wake.py)
  sleep 10  # a new role takes a few seconds to be usable by Lambda
  account=$(aws sts get-caller-identity --query Account --output text)
  ip=$(address PublicIp)
  aws lambda create-function --function-name $WAKE --runtime python3.13 --handler wake.handler --timeout 15 \
    --role "arn:aws:iam::$account:role/$WAKE" --zip-file fileb://logs/wake.zip \
    --environment "Variables={HOST=${ip//./-}.sslip.io,INSTANCE=$NAME}"
  aws lambda wait function-active-v2 --function-name $WAKE
  aws lambda create-function-url-config --function-name $WAKE --auth-type NONE
  for action in InvokeFunctionUrl InvokeFunction; do  # a public function URL needs both
    extra=(); [ $action = InvokeFunctionUrl ] && extra=(--function-url-auth-type NONE)
    aws lambda add-permission --function-name $WAKE --statement-id public-$action --action lambda:$action \
      --principal '*' "${extra[@]}"
  done
  ;&
status)
  echo "link to share: $(aws lambda get-function-url-config --function-name $WAKE --query FunctionUrl --output text)"
  id=$(instance)
  echo "instance $id: $(aws ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].State.Name' \
    --output text), https://$(address PublicIp | tr . -).sslip.io (stops after 30 idle minutes)"
  aws ec2 get-console-output --instance-id "$id" --latest --output text | tail -${LINES_SHOWN:-40} || true
  ;;
update)
  send_code
  aws iam put-role-policy --role-name $NAME --policy-name main --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$INSTANCE_POLICY]}"
  (cd deploy && pixi exec --spec python python -m zipfile -c ../logs/wake.zip wake.py)
  aws lambda update-function-code --function-name $WAKE --zip-file fileb://logs/wake.zip --query LastModified
  if [ "$(aws ec2 describe-instances --instance-ids "$(instance)" --query 'Reservations[0].Instances[0].State.Name' --output text)" = running ]; then
    aws ec2 reboot-instances --instance-ids "$(instance)"
    echo "rebooting $(instance): it rebuilds and is back in a few minutes at the same address"
  else
    echo "$(instance) is not running: it gets the new package at its next start"
  fi
  ;;
domain)
  zone=$(aws route53 list-hosted-zones-by-name --dns-name $DOMAIN --query "HostedZones[?Name=='$DOMAIN.'].Id | [0]" --output text)
  [ "$zone" != None ] || { echo "no Route 53 zone for $DOMAIN yet (registering it creates one)"; exit 1; }
  records() {  # UPSERT a list of record sets in the zone
    aws route53 change-resource-record-sets --hosted-zone-id "$zone" --change-batch \
      "{\"Changes\":[$(echo "$1" | sed 's/^/{"Action":"UPSERT","ResourceRecordSet":/; s/$/}/' | paste -sd,)]}" --query ChangeInfo.Id --output text
  }
  # the certificate (CloudFront's are in us-east-1), validated by a DNS record per name
  cert=$(aws acm list-certificates --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
  if [ "$cert" = None ]; then
    cert=$(aws acm request-certificate --domain-name $DOMAIN --subject-alternative-names www.$DOMAIN \
      --validation-method DNS --query CertificateArn --output text)
    sleep 15  # its validation records take a moment to be listed
  fi
  records "$(aws acm describe-certificate --certificate-arn "$cert" --output json --query \
    'Certificate.DomainValidationOptions[].ResourceRecord' | pixi exec --spec jq jq -c 'unique[] |
    {Name, Type, TTL: 300, ResourceRecords: [{Value}]}')"
  aws acm wait certificate-validated --certificate-arn "$cert"
  echo "certificate $cert: $(aws acm describe-certificate --certificate-arn "$cert" --query Certificate.Status --output text)"
  if [ "$(distribution Id)" = None ]; then
    policy() { aws cloudfront list-$1-policies --type managed --query "${2}PolicyList.Items[?${2}Policy.${2}PolicyConfig.Name=='$3'].${2}Policy.Id | [0]" --output text; }
    nocache=$(policy cache Cache Managed-CachingDisabled) bycontrol=$(policy cache Cache UseOriginCacheControlHeaders-QueryStrings)
    viewer=$(policy origin-request OriginRequest Managed-AllViewerExceptHostHeader)
    for id in "$nocache" "$bycontrol" "$viewer"; do [ "$id" != None ] || { echo "a managed CloudFront policy was not found"; exit 1; }; done
    lambda=$(aws lambda get-function-url-config --function-name $WAKE --query FunctionUrl --output text | sed 's|https://||; s|/$||')
    origin() {  # id, host, read timeout, extra fields
      echo "{\"Id\":\"$1\",\"DomainName\":\"$2\",\"ConnectionAttempts\":1,\"ConnectionTimeout\":3,${4:-}
        \"CustomOriginConfig\":{\"HTTPPort\":80,\"HTTPSPort\":443,\"OriginProtocolPolicy\":\"https-only\",
        \"OriginSslProtocols\":{\"Quantity\":1,\"Items\":[\"TLSv1.2\"]},\"OriginReadTimeout\":$3,\"OriginKeepaliveTimeout\":5}}"
    }
    behavior() {  # path, origin, cache policy, methods
      echo "{\"PathPattern\":\"$1\",\"TargetOriginId\":\"$2\",\"ViewerProtocolPolicy\":\"redirect-to-https\",\"Compress\":true,
        \"CachePolicyId\":\"$3\",\"OriginRequestPolicyId\":\"$viewer\",\"AllowedMethods\":{$4,
        \"CachedMethods\":{\"Quantity\":2,\"Items\":[\"GET\",\"HEAD\"]}}}" | sed 's/"PathPattern":"",//'
    }
    get='"Quantity":2,"Items":["GET","HEAD"]' all='"Quantity":7,"Items":["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"]'
    cat > logs/distribution.json <<JSON
{"CallerReference": "$NAME-$(date +%s)", "Comment": "SWOT's Up", "Enabled": true, "HttpVersion": "http2and3",
 "IsIPV6Enabled": true, "PriceClass": "PriceClass_All",
 "Aliases": {"Quantity": 2, "Items": ["$DOMAIN", "www.$DOMAIN"]},
 "ViewerCertificate": {"ACMCertificateArn": "$cert", "SSLSupportMethod": "sni-only", "MinimumProtocolVersion": "TLSv1.2_2021"},
 "Origins": {"Quantity": 2, "Items": [
   $(origin ec2 "$(address PublicIp | tr . -).sslip.io" ${ORIGIN_TIMEOUT:-60}),
   $(origin wake "$lambda" 30 '"CustomHeaders":{"Quantity":1,"Items":[{"HeaderName":"X-Swot-Front","HeaderValue":"1"}]},')]},
 "OriginGroups": {"Quantity": 1, "Items": [{"Id": "ec2-or-wake",
   "FailoverCriteria": {"StatusCodes": {"Quantity": 2, "Items": [502, 503]}},
   "Members": {"Quantity": 2, "Items": [{"OriginId": "ec2"}, {"OriginId": "wake"}]}}]},
 "DefaultCacheBehavior": $(behavior "" ec2-or-wake $nocache "$get"),
 "CacheBehaviors": {"Quantity": 4, "Items": [
   $(behavior /api/event ec2 $nocache "$all"),
   $(behavior "/api/tile/*" ec2-or-wake $bycontrol "$get"),
   $(behavior "/api/detail/*" ec2-or-wake $bycontrol "$get"),
   $(behavior /api/world.png ec2-or-wake $bycontrol "$get")]},
 "CustomErrorResponses": {"Quantity": 3, "Items": [{"ErrorCode": 502, "ErrorCachingMinTTL": 0},
   {"ErrorCode": 503, "ErrorCachingMinTTL": 0}, {"ErrorCode": 504, "ErrorCachingMinTTL": 0}]}}
JSON
    aws cloudfront create-distribution --distribution-config file://logs/distribution.json --query Distribution.Id --output text
  fi
  front=$(distribution DomainName)
  records "$(for name in $DOMAIN www.$DOMAIN; do for type in A AAAA; do
    echo "{\"Name\":\"$name\",\"Type\":\"$type\",\"AliasTarget\":{\"HostedZoneId\":\"Z2FDTNDATAQYW2\",\"DNSName\":\"$front\",\"EvaluateTargetHealth\":false}}"
  done; done)"
  ip=$(address PublicIp)
  aws lambda update-function-configuration --function-name $WAKE --query LastModified \
    --environment "Variables={HOST=${ip//./-}.sslip.io,INSTANCE=$NAME,FRONT=https://$DOMAIN/}"
  echo "https://$DOMAIN/ -> $front (CloudFront takes ~5-15 min to deploy); run update so the page's links use it"
  ;;
down)
  aws lambda delete-function --function-name $WAKE || true
  id=$(instance)
  if [ -n "$id" ]; then aws ec2 terminate-instances --instance-ids $id; aws ec2 wait instance-terminated --instance-ids $id; fi
  [ "$(address AllocationId)" = None ] || aws ec2 release-address --allocation-id "$(address AllocationId)"
  aws ec2 delete-security-group --group-name $NAME || true
  aws iam remove-role-from-instance-profile --instance-profile-name $NAME --role-name $NAME || true
  aws iam delete-instance-profile --instance-profile-name $NAME || true
  for r in $NAME $WAKE; do
    aws iam delete-role-policy --role-name $r --policy-name main || true
    aws iam delete-role-policy --role-name $r --policy-name read || true  # the first version's name
    aws iam delete-role --role-name $r || true
  done
  aws s3 rm s3://$BUCKET/$CODE || true
  ;;
esac
