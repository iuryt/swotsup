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
# STAGE=dev: a second instance for testing branches (swot-explorer-dev: any branch, sent as it is; the production one
# gets only what was tested there), at its own https://<ip>.sslip.io, without link or domain (`up` starts it, it sleeps
# when idle as the other), with the production's role and security group; its usage and the map's events of pages
# opened with ?debug=1 go to telemetry/swot-explorer-dev/ every minute.
#   STAGE=dev sbatch -o logs/ec2-%j.out slurm/ec2.sh up|update|status|start|down
# Both read the daily maps (app/rasters.py) from swot_cloud's Arraylake repository.
#SBATCH -p batch -c 1 --mem 2G -t 30
set -euo pipefail
cd "${SLURM_SUBMIT_DIR:-.}"

STAGE=${STAGE:-prod}
NAME=swot-explorer WAKE=swot-explorer-wake TYPE=${TYPE:-c7i.2xlarge} BUCKET=iuryt-shared-east
STORE=icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2 CODE=deploy/swot_explorer/app.tar.gz
TELEMETRY=telemetry/swot-explorer  # where the instance writes the anonymous usage (write only)
ROLE=$NAME  # the production's role (and security group), the dev instance's too
# the heavy requests (series) as a Lambda function with the server's image (app/lambda_work.py): its image repository
# and role; the function swot-explorer-work (and -dev), its image pushed and updated by each instance at boot
WORK=swot-explorer-work
if [ "$STAGE" = dev ]; then
  NAME=swot-explorer-dev CODE=deploy/swot_explorer/dev/app.tar.gz TELEMETRY=telemetry/swot-explorer-dev
fi
DOMAIN=${DOMAIN:-swotsup.org}
export AWS_REGION=us-east-1
aws() { pixi exec --spec awscli aws --profile "${DEPLOY_PROFILE:-swot-deploy}" "$@"; }
instance() {
  aws ec2 describe-instances --query 'Reservations[].Instances[].InstanceId' --output text \
    --filters Name=tag:Name,Values=$NAME Name=instance-state-name,Values=pending,running,stopping,stopped
}
address() { aws ec2 describe-addresses --filters Name=tag:Name,Values=$NAME --query "Addresses[0].$1" --output text; }
distribution() { aws cloudfront list-distributions --query "DistributionList.Items[?Aliases.Items[0]=='$DOMAIN'].$1 | [0]" --output text; }
send_code() {  # build context (Dockerfile at the root), the boot script, the link to share (copied links start with it)
  # and the stage (what boot.sh does differently)
  if [ "$STAGE" = dev ]; then [ "$(address PublicIp)" = None ] && : > logs/public_url || echo "https://$(address PublicIp | tr . -).sslip.io/" > logs/public_url
  elif [ "$(distribution Id)" != None ]; then echo "https://$DOMAIN/" > logs/public_url
  else aws lambda get-function-url-config --function-name $WAKE --query FunctionUrl --output text > logs/public_url 2>/dev/null || : > logs/public_url; fi
  echo "$STAGE" > logs/stage
  # settings of the server to try (dev), e.g. SERVER_ENV="SWOT_LOADS=4 WEB_CONCURRENCY=2": one per line, for docker
  printf '%s\n' ${SERVER_ENV:-} > logs/server.env
  tar -czf logs/app.tar.gz --exclude __pycache__ Dockerfile pyproject.toml pixi.lock app -C deploy boot.sh -C ../logs public_url stage server.env
  aws s3 cp logs/app.tar.gz s3://$BUCKET/$CODE
}
# what the instances may read: the S3 store, the app packages (both stages'), and the secrets under
# /swot-explorer/ in SSM (the Arraylake token, put there by the user in the console); and write: the anonymous usage
# (both stages'), nothing else
INSTANCE_POLICY="
    {\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$BUCKET/$STORE/*\",\"arn:aws:s3:::$BUCKET/deploy/swot_explorer/*\"]},
    {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\",
     \"Condition\":{\"StringLike\":{\"s3:prefix\":[\"$STORE/*\"]}}},
    {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:us-east-1:*:parameter/swot-explorer/*\"},
    {\"Effect\":\"Allow\",\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/telemetry/swot-explorer*\"},
    {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$BUCKET\",
     \"Condition\":{\"StringLike\":{\"s3:prefix\":\"telemetry/swot-explorer*\"}}},
    {\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:InitiateLayerUpload\",\"ecr:UploadLayerPart\",
     \"ecr:CompleteLayerUpload\",\"ecr:PutImage\",\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],
     \"Resource\":\"arn:aws:ecr:us-east-1:*:repository/$WORK\"},
    {\"Effect\":\"Allow\",\"Action\":[\"lambda:InvokeFunction\",\"lambda:UpdateFunctionCode\",\"lambda:GetFunction\"],
     \"Resource\":\"arn:aws:lambda:us-east-1:*:function:$WORK*\"}"
role() {  # name, service that assumes it, inline policy
  aws iam create-role --role-name "$1" --assume-role-policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}"
  aws iam put-role-policy --role-name "$1" --policy-name main --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$3]}"
}

case $1 in
up)
  [ -z "$(instance)" ] || { echo "already there: $(instance)"; exit 1; }
  aws ec2 allocate-address --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]"
  send_code
  if [ "$STAGE" = dev ]; then  # the production's role (its policy as now) and security group
    aws iam put-role-policy --role-name $ROLE --policy-name main --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$INSTANCE_POLICY]}"
    sg=$(aws ec2 describe-security-groups --group-names $ROLE --query 'SecurityGroups[0].GroupId' --output text)
  else
    role $NAME ec2.amazonaws.com "$INSTANCE_POLICY"
    aws iam create-instance-profile --instance-profile-name $NAME
    aws iam add-role-to-instance-profile --instance-profile-name $NAME --role-name $NAME
    sg=$(aws ec2 create-security-group --group-name $NAME --description "SWOT explorer, web only" --query GroupId --output text)
    for port in 80 443; do
      aws ec2 authorize-security-group-ingress --group-id "$sg" --protocol tcp --port $port --cidr 0.0.0.0/0
    done
  fi
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
    --iam-instance-profile Name=$ROLE --user-data file://logs/ec2-user-data.sh \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3}' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" --query 'Instances[0].InstanceId' --output text
  aws ec2 wait instance-running --instance-ids "$(instance)"
  # the fixed address, there long before boot.sh asks for it (installing docker comes first)
  aws ec2 associate-address --instance-id "$(instance)" --allocation-id "$(address AllocationId)"
  [ "$STAGE" = dev ] && { echo "dev: https://$(address PublicIp | tr . -).sslip.io (in a few minutes)"; exit 0; }
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
  [ "$STAGE" = dev ] || echo "link to share: $(aws lambda get-function-url-config --function-name $WAKE --query FunctionUrl --output text)"
  id=$(instance)
  echo "instance $id: $(aws ec2 describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].State.Name' \
    --output text), https://$(address PublicIp | tr . -).sslip.io (stops after 30 idle minutes)"
  aws ec2 get-console-output --instance-id "$id" --latest --output text | tail -${LINES_SHOWN:-40} || true
  ;;
update)
  # production: only main, as committed (the dev instance takes any branch as it is)
  if [ "$STAGE" = prod ] && { [ "$(git branch --show-current)" != main ] || ! git diff --quiet HEAD -- app deploy Dockerfile pixi.lock; }; then
    echo "production gets main as committed: on $(git branch --show-current), test with STAGE=dev"; exit 1
  fi
  send_code
  aws iam put-role-policy --role-name $ROLE --policy-name main --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[$INSTANCE_POLICY]}"
  if [ "$STAGE" = prod ]; then
    (cd deploy && pixi exec --spec python python -m zipfile -c ../logs/wake.zip wake.py)
    aws lambda update-function-code --function-name $WAKE --zip-file fileb://logs/wake.zip --query LastModified
  fi
  if [ "$(aws ec2 describe-instances --instance-ids "$(instance)" --query 'Reservations[0].Instances[0].State.Name' --output text)" = running ]; then
    aws ec2 reboot-instances --instance-ids "$(instance)"
    echo "rebooting $(instance): it rebuilds and is back in a few minutes at the same address"
  else
    echo "$(instance) is not running: it gets the new package at its next start"
  fi
  ;;
work)  # the Lambda function for the heavy requests: repository, role, and the function once its image is there
  aws ecr describe-repositories --repository-names $WORK >/dev/null 2>&1 || {
    aws ecr create-repository --repository-name $WORK --query repository.repositoryUri --output text
    aws ecr put-lifecycle-policy --repository-name $WORK --lifecycle-policy-text \
      '{"rules":[{"rulePriority":1,"selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":4},"action":{"type":"expire"}}]}' >/dev/null
  }
  aws iam get-role --role-name $WORK >/dev/null 2>&1 || role $WORK lambda.amazonaws.com "
    {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:us-east-1:*:parameter/swot-explorer/*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"}" >/dev/null
  fn=$WORK$([ "$STAGE" = dev ] && echo -dev || true)  # (a failed test as the last status would end the script)
  uri=$(aws ecr describe-repositories --repository-names $WORK --query 'repositories[0].repositoryUri' --output text):$STAGE
  if ! aws ecr describe-images --repository-name $WORK --image-ids imageTag=$STAGE >/dev/null 2>&1; then
    echo "no image $uri yet: update the $STAGE instance (it pushes it at boot), then run this again"; exit 0
  fi
  # at most this many machines at once (the account allows 1000): a ceiling on what it can cost; beyond it, the server
  # works the requests out itself
  ceiling=$([ "$STAGE" = dev ] && echo 50 || echo 100)
  if aws lambda get-function --function-name $fn >/dev/null 2>&1; then
    aws lambda update-function-code --function-name $fn --image-uri $uri --query LastUpdateStatus --output text
    aws lambda put-function-concurrency --function-name $fn --reserved-concurrent-executions $ceiling --output text
  else  # 3 GB (the account's most, ~1.8 vCPU): a 250 m series of the whole mission; the server's entry point, then the Lambda client
    aws lambda create-function --function-name $fn --package-type Image --code ImageUri=$uri \
      --role "$(aws iam get-role --role-name $WORK --query Role.Arn --output text)" --memory-size 3008 --timeout 120 \
      --image-config '{"EntryPoint":["/bin/bash","-c","source /shell-hook.sh && exec \"$@\"","--"],"Command":["python","-m","awslambdaric","lambda_work.handler"],"WorkingDirectory":"/srv/app"}' \
      --environment "Variables={SWOT_TOKEN_PARAMETER=/swot-explorer/arraylake-token,OMP_NUM_THREADS=1}" \
      --query State --output text
  fi
  ;;
resize)  # another instance type (TYPE=...), e.g. a larger one for a meeting: stop it, change it, start it again
  id=$(instance)
  aws ec2 stop-instances --instance-ids "$id" >/dev/null && aws ec2 wait instance-stopped --instance-ids "$id"
  aws ec2 modify-instance-attribute --instance-id "$id" --instance-type "{\"Value\": \"$TYPE\"}"
  aws ec2 start-instances --instance-ids "$id" --query 'StartingInstances[0].CurrentState.Name' --output text
  echo "$id is now $TYPE: back in a few minutes at the same address"
  ;;
start)  # wake it (the dev instance has no link that does)
  aws ec2 start-instances --instance-ids "$(instance)" --query 'StartingInstances[0].CurrentState.Name' --output text
  echo "https://$(address PublicIp | tr . -).sslip.io in a few minutes"
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
    nocache=$(policy cache Cache Managed-CachingDisabled) viewer=$(policy origin-request OriginRequest Managed-AllViewerExceptHostHeader)
    for id in "$nocache" "$viewer"; do [ "$id" != None ] || { echo "a managed CloudFront policy was not found"; exit 1; }; done
    # tiles: cached by their whole URL as long as the server says; no header in the key (the managed policies that
    # follow Cache-Control put the Host in it, and so send the domain's to the origins, which refuse it: 403)
    bycontrol=$(aws cloudfront list-cache-policies --type custom --output text \
      --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='swot-tiles'].CachePolicy.Id | [0]")
    [ "$bycontrol" != None ] || bycontrol=$(aws cloudfront create-cache-policy --query CachePolicy.Id --output text \
      --cache-policy-config '{"Name":"swot-tiles","DefaultTTL":0,"MaxTTL":86400,"MinTTL":0,
        "ParametersInCacheKeyAndForwardedToOrigin":{"EnableAcceptEncodingGzip":true,"EnableAcceptEncodingBrotli":true,
        "HeadersConfig":{"HeaderBehavior":"none"},"CookiesConfig":{"CookieBehavior":"none"},
        "QueryStringsConfig":{"QueryStringBehavior":"all"}}}')
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
 "CacheBehaviors": {"Quantity": 5, "Items": [
   $(behavior /api/event ec2 $nocache "$all"),
   $(behavior "/api/tile/*" ec2-or-wake $bycontrol "$get"),
   $(behavior "/api/basemap/*" ec2-or-wake $bycontrol "$get"),
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
  [ "$STAGE" = dev ] || aws lambda delete-function --function-name $WAKE || true
  id=$(instance)
  if [ -n "$id" ]; then aws ec2 terminate-instances --instance-ids $id; aws ec2 wait instance-terminated --instance-ids $id; fi
  [ "$(address AllocationId)" = None ] || aws ec2 release-address --allocation-id "$(address AllocationId)"
  [ "$STAGE" = dev ] && { aws s3 rm s3://$BUCKET/$CODE || true; exit 0; }  # the role and security group are the production's
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
