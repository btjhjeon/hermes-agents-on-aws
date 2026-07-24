# Hermes Agent on AWS - 배포 가이드

Hermes Agent를 AWS의 private EC2에 독립적으로 배포하는 절차입니다. 자동
배포는 [installer.py](./installer.py)를 권장하며, 이 문서는
installer가 생성하는 리소스와 수동 확인 절차를 14개 Phase로 설명합니다.

## 아키텍처 개요

```text
                                     AWS
┌──────────────────────────────────────────────────────────────────────┐
│ Browser                                                              │
│   -> CloudFront HTTPS                                                │
│   -> VPC Origin -> Internal ALB HTTP + origin secret                 │
│   -> private EC2:9119                                                │
│   -> hermes-dashboard.service                                        │
│                                                                      │
│ Telegram                                                             │
│   <-> Hermes messaging gateway on EC2                                │
│   <-> NAT Gateway <-> Telegram Bot API                               │
│                                                                      │
│ Hermes Agent                                                         │
│   -> Bedrock Runtime interface endpoint -> Amazon Bedrock             │
│   -> Bedrock Agent Runtime endpoint -> Knowledge Base                 │
│   -> S3 docs/ + OpenSearch Serverless                                 │
│                                                                      │
│ Administration                                                       │
│   -> AWS Systems Manager Session Manager -> private EC2               │
└──────────────────────────────────────────────────────────────────────┘
```

핵심 경계는 다음과 같습니다.

- Dashboard는 `hermes dashboard`를 실행하는 웹 서비스입니다.
- messaging gateway는 Telegram, Discord, WhatsApp 등을 처리하는 별도
  프로세스입니다.
- ALB와 CloudFront는 Dashboard에만 필요합니다.
- EC2에는 public IP와 SSH ingress를 만들지 않고 SSM으로 관리합니다.
- Bedrock 호출은 Instance Profile과 private VPC Endpoint를 사용합니다.
- Telegram polling과 package update에는 NAT Gateway가 필요합니다.

## 권장 자동 설치

```bash
python3 -m pip install -r requirements.txt
aws sts get-caller-identity
python3 installer.py
```

첫 실행은 CloudFront URL을 만들고 Dashboard를 중지된 상태로 둡니다.
`assets/hermes-deployment-info.md`의 callback URL을
https://portal.nousresearch.com/local-dashboards 에 등록한 뒤 발급받은 client
ID로 재실행합니다.

```bash
python3 installer.py \
  --dashboard-oauth-client-id agent:<ID>
```

주요 옵션:

```bash
python3 installer.py \
  --region us-west-2 \
  --instance-type t3.medium \
  --model-id global.anthropic.claude-sonnet-5
```

```text
--dashboard-oauth-client-id agent:<ID>
--disable-knowledge-base
--skip-browser
--vpc-cidr 10.25.0.0/16
```

공개 ALB HTTP 접속을 만들던 `--disable-cloudfront`는 보안상 지원하지 않습니다.

진행 중 실패해도 생성된 resource ID는
`assets/hermes-deployment.json`에 보존됩니다. 원인을 해결한 뒤 같은 옵션으로
재실행하면 관리 태그와 state를 검증해 이어서 구성합니다.

## 공통 변수

수동 확인 명령의 placeholder는 환경에 맞게 설정합니다.

```bash
export AWS_REGION=us-west-2
export PROJECT=hermes
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export VPC_CIDR=10.25.0.0/16
export PUBLIC_CIDR_A=10.25.1.0/24
export PUBLIC_CIDR_B=10.25.2.0/24
export PRIVATE_CIDR_A=10.25.11.0/24
export PRIVATE_CIDR_B=10.25.12.0/24
export AZ_A="${AWS_REGION}a"
export AZ_B="${AWS_REGION}b"
```

CIDR은 기존 VPC, VPN 및 사내 network와 겹치지 않아야 합니다. installer는
기본 후보 중 겹치지 않는 `/16`을 선택합니다.

## Phase 1: 사전 준비

### AWS 인증 확인

```bash
aws sts get-caller-identity
aws configure get region
```

배포 주체에는 EC2, IAM, ELBv2, CloudFront(VPC origin 포함), S3, Bedrock,
OpenSearch Serverless 및 Systems Manager resource를 생성하고 tag할 권한이
필요합니다. 첫 VPC origin 생성 시 service-linked role
`AWSServiceRoleForCloudFrontVPCOrigin`이 자동 생성됩니다. Knowledge Base를
끄면 S3/AOSS/Bedrock Knowledge Base 생성 권한은 생략할 수 있습니다.

### Bedrock 모델 확인

```bash
aws bedrock list-foundation-models \
  --region "$AWS_REGION" \
  --query 'modelSummaries[].modelId' \
  --output text
```

사용할 model 또는 cross-region inference profile이 계정에서 허용됐는지
확인합니다.

### 로컬 도구

```bash
python3 --version
aws --version
python3 -m pip install -r requirements.txt
```

## Phase 2: IAM Role과 Instance Profile

EC2 trust policy:

```bash
cat > /tmp/hermes-ec2-trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
JSON
```

Role과 Instance Profile을 만듭니다.

```bash
aws iam create-role \
  --role-name "${PROJECT}-bedrock-role" \
  --assume-role-policy-document file:///tmp/hermes-ec2-trust.json

aws iam attach-role-policy \
  --role-name "${PROJECT}-bedrock-role" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile \
  --instance-profile-name "${PROJECT}-bedrock-profile"

aws iam add-role-to-instance-profile \
  --instance-profile-name "${PROJECT}-bedrock-profile" \
  --role-name "${PROJECT}-bedrock-role"
```

EC2 inline policy에는 최소한 다음 action이 필요합니다.

```json
{
  "Effect": "Allow",
  "Action": [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:ListFoundationModels",
    "bedrock:GetFoundationModel",
    "bedrock:ListInferenceProfiles",
    "bedrock:GetInferenceProfile"
  ],
  "Resource": "*"
}
```

Knowledge Base를 사용하면 추가합니다.

```json
[
  {
    "Effect": "Allow",
    "Action": ["bedrock:GetKnowledgeBase", "bedrock:Retrieve"],
    "Resource": "arn:aws:bedrock:<REGION>:<ACCOUNT_ID>:knowledge-base/*"
  },
  {
    "Effect": "Allow",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::<KNOWLEDGE_BASE_BUCKET>/docs/*"
  }
]
```

운영 환경에서는 model, Knowledge Base 및 S3 ARN을 실제 resource로
제한하세요. static AWS access key를 EC2에 저장하지 않습니다.

## Phase 3: VPC와 Subnet

VPC를 생성하고 DNS를 활성화합니다.

```bash
VPC_ID=$(aws ec2 create-vpc \
  --cidr-block "$VPC_CIDR" \
  --tag-specifications \
    "ResourceType=vpc,Tags=[{Key=Name,Value=${PROJECT}-vpc},{Key=Project,Value=${PROJECT}}]" \
  --query 'Vpc.VpcId' \
  --output text)

aws ec2 modify-vpc-attribute \
  --vpc-id "$VPC_ID" \
  --enable-dns-support '{"Value":true}'

aws ec2 modify-vpc-attribute \
  --vpc-id "$VPC_ID" \
  --enable-dns-hostnames '{"Value":true}'
```

서로 다른 AZ에 public/private subnet을 만듭니다.

```bash
PUBLIC_SUBNET_A=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block "$PUBLIC_CIDR_A" \
  --availability-zone "$AZ_A" \
  --query 'Subnet.SubnetId' --output text)

PUBLIC_SUBNET_B=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block "$PUBLIC_CIDR_B" \
  --availability-zone "$AZ_B" \
  --query 'Subnet.SubnetId' --output text)

PRIVATE_SUBNET_A=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block "$PRIVATE_CIDR_A" \
  --availability-zone "$AZ_A" \
  --query 'Subnet.SubnetId' --output text)

PRIVATE_SUBNET_B=$(aws ec2 create-subnet \
  --vpc-id "$VPC_ID" \
  --cidr-block "$PRIVATE_CIDR_B" \
  --availability-zone "$AZ_B" \
  --query 'Subnet.SubnetId' --output text)
```

Internet Gateway와 public route를 설정합니다.

```bash
IGW_ID=$(aws ec2 create-internet-gateway \
  --query 'InternetGateway.InternetGatewayId' \
  --output text)

aws ec2 attach-internet-gateway \
  --internet-gateway-id "$IGW_ID" \
  --vpc-id "$VPC_ID"

PUBLIC_RT_ID=$(aws ec2 create-route-table \
  --vpc-id "$VPC_ID" \
  --query 'RouteTable.RouteTableId' \
  --output text)

aws ec2 create-route \
  --route-table-id "$PUBLIC_RT_ID" \
  --destination-cidr-block 0.0.0.0/0 \
  --gateway-id "$IGW_ID"

aws ec2 associate-route-table \
  --route-table-id "$PUBLIC_RT_ID" \
  --subnet-id "$PUBLIC_SUBNET_A"

aws ec2 associate-route-table \
  --route-table-id "$PUBLIC_RT_ID" \
  --subnet-id "$PUBLIC_SUBNET_B"
```

## Phase 4: NAT Gateway

private EC2가 Telegram API, package repository와 Hermes update endpoint에
접근하도록 NAT Gateway를 public subnet에 생성합니다.

```bash
EIP_ALLOC_ID=$(aws ec2 allocate-address \
  --domain vpc \
  --query AllocationId \
  --output text)

NAT_ID=$(aws ec2 create-nat-gateway \
  --subnet-id "$PUBLIC_SUBNET_A" \
  --allocation-id "$EIP_ALLOC_ID" \
  --query 'NatGateway.NatGatewayId' \
  --output text)

aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_ID"

PRIVATE_RT_ID=$(aws ec2 create-route-table \
  --vpc-id "$VPC_ID" \
  --query 'RouteTable.RouteTableId' \
  --output text)

aws ec2 create-route \
  --route-table-id "$PRIVATE_RT_ID" \
  --destination-cidr-block 0.0.0.0/0 \
  --nat-gateway-id "$NAT_ID"

aws ec2 associate-route-table \
  --route-table-id "$PRIVATE_RT_ID" \
  --subnet-id "$PRIVATE_SUBNET_A"

aws ec2 associate-route-table \
  --route-table-id "$PRIVATE_RT_ID" \
  --subnet-id "$PRIVATE_SUBNET_B"
```

고가용성이 필요하면 AZ별 NAT Gateway와 private route table을 사용합니다.
installer의 기본 구성은 비용을 줄이기 위해 NAT Gateway 하나를 사용합니다.

## Phase 5: Security Group과 VPC Endpoint

Security Group 역할:

| Security Group | Inbound |
|---|---|
| EC2 | ALB SG에서 TCP `9119` |
| ALB | CloudFront origin-facing prefix list에서 TCP `443` |
| Endpoint | EC2 SG에서 TCP `443` |

```bash
EC2_SG_ID=$(aws ec2 create-security-group \
  --group-name "${PROJECT}-ec2-sg" \
  --description "Hermes Agent EC2" \
  --vpc-id "$VPC_ID" \
  --query GroupId --output text)

ALB_SG_ID=$(aws ec2 create-security-group \
  --group-name "${PROJECT}-alb-sg" \
  --description "Hermes Agent ALB" \
  --vpc-id "$VPC_ID" \
  --query GroupId --output text)

ENDPOINT_SG_ID=$(aws ec2 create-security-group \
  --group-name "${PROJECT}-endpoint-sg" \
  --description "Hermes Agent endpoints" \
  --vpc-id "$VPC_ID" \
  --query GroupId --output text)

aws ec2 authorize-security-group-ingress \
  --group-id "$EC2_SG_ID" \
  --protocol tcp \
  --port 9119 \
  --source-group "$ALB_SG_ID"

aws ec2 authorize-security-group-ingress \
  --group-id "$ENDPOINT_SG_ID" \
  --protocol tcp \
  --port 443 \
  --source-group "$EC2_SG_ID"

CF_PREFIX_LIST_ID=$(aws ec2 describe-managed-prefix-lists \
  --filters \
    Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' \
  --output text)

test "$CF_PREFIX_LIST_ID" != "None"
aws ec2 authorize-security-group-ingress \
  --group-id "$ALB_SG_ID" \
  --ip-permissions \
    "IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=${CF_PREFIX_LIST_ID}}]"
```

Bedrock Runtime interface endpoint:

```bash
BEDROCK_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
  --vpc-id "$VPC_ID" \
  --vpc-endpoint-type Interface \
  --service-name "com.amazonaws.${AWS_REGION}.bedrock-runtime" \
  --subnet-ids "$PRIVATE_SUBNET_A" "$PRIVATE_SUBNET_B" \
  --security-group-ids "$ENDPOINT_SG_ID" \
  --private-dns-enabled \
  --query 'VpcEndpoint.VpcEndpointId' \
  --output text)
```

Knowledge Base retrieve를 사용하면 Agent Runtime endpoint도 생성합니다.

```bash
KB_RUNTIME_ENDPOINT_ID=$(aws ec2 create-vpc-endpoint \
  --vpc-id "$VPC_ID" \
  --vpc-endpoint-type Interface \
  --service-name "com.amazonaws.${AWS_REGION}.bedrock-agent-runtime" \
  --subnet-ids "$PRIVATE_SUBNET_A" "$PRIVATE_SUBNET_B" \
  --security-group-ids "$ENDPOINT_SG_ID" \
  --private-dns-enabled \
  --query 'VpcEndpoint.VpcEndpointId' \
  --output text)
```

SSH `22` ingress는 만들지 않습니다.

## Phase 6: EC2 인스턴스

최신 Amazon Linux 2023 AMI를 조회합니다.

```bash
AMI_ID=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' \
  --output text \
  --region "$AWS_REGION")
```

EC2는 다음 속성을 사용합니다.

- private subnet, public IP 없음
- encrypted gp3 EBS
- IMDSv2 required
- Hermes Bedrock Instance Profile
- EC2 Security Group

```bash
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type t3.medium \
  --iam-instance-profile "Name=${PROJECT}-bedrock-profile" \
  --subnet-id "$PRIVATE_SUBNET_A" \
  --security-group-ids "$EC2_SG_ID" \
  --no-associate-public-ip-address \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings \
    'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Name,Value=${PROJECT}},{Key=Project,Value=${PROJECT}}]" \
  --query 'Instances[0].InstanceId' \
  --output text)

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
```

수동 실행 시 Hermes 설치와 Dashboard service를 user data에 넣거나 다음
Phase를 SSM으로 수행합니다. 저장소의 [create-instance.sh](./create-instance.sh)는
간단한 수동 EC2 생성 흐름을 제공합니다. OAuth 값 없이 실행하면 Dashboard
service는 비활성 상태로 설치됩니다. public URL과 client ID를 이미 준비한
경우에는 두 값을 함께 전달할 수 있습니다.

```bash
DASHBOARD_OAUTH_CLIENT_ID=agent:<ID> \
DASHBOARD_PUBLIC_URL=https://<CLOUDFRONT_DOMAIN> \
SUBNET_ID="$PRIVATE_SUBNET_A" \
SG_ID="$EC2_SG_ID" \
./create-instance.sh
```

## Phase 7: Hermes Agent 설치

SSM으로 접속합니다.

```bash
aws ssm start-session \
  --target "$INSTANCE_ID" \
  --region "$AWS_REGION"
```

필수 package를 설치하고 Hermes 공식 installer를 `ec2-user`로 실행합니다.

```bash
sudo dnf install -y curl git

sudo -u ec2-user -H bash -lc \
  'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive --skip-setup'

sudo -u ec2-user -H bash -lc 'hermes --version'
```

browser tool을 사용하면 installer가 요구하는 Chromium과 shared library도
설치해야 합니다. 자동 installer에서는 `--skip-browser`를 지정하지 않는 한
처리합니다.

## Phase 8: Bedrock 모델 설정

`/home/ec2-user/.hermes/config.yaml`을 다음 구조로 설정합니다.

```yaml
model:
  default: global.anthropic.claude-sonnet-5
  provider: bedrock
  base_url: https://bedrock-runtime.us-west-2.amazonaws.com
bedrock:
  region: us-west-2
```

권한과 설정을 확인합니다.

```bash
sudo su - ec2-user
hermes config show
hermes config check
hermes doctor
hermes chat -q "한 문장으로 연결 상태를 알려줘"
```

model ID와 endpoint Region을 일치시키고, credential은 Instance Profile에서
가져오도록 둡니다.

## Phase 9: Dashboard systemd 서비스

Dashboard는 messaging gateway가 아닌 다음 process를 실행합니다.

```bash
/home/ec2-user/.local/bin/hermes dashboard \
  --host 0.0.0.0 \
  --port 9119 \
  --no-open
```

공개 Dashboard에는 Hermes가 권장하는 Nous OAuth를 사용합니다. CloudFront
배포 후 다음 값을 `~/.hermes/config.yaml`에 설정합니다.

```yaml
dashboard:
  oauth:
    client_id: agent:<ID>
  public_url: https://<CLOUDFRONT_DOMAIN>
```

Nous Portal에는
`https://<CLOUDFRONT_DOMAIN>/auth/callback`을 redirect URI로 등록합니다. 자동
installer는 OAuth client ID가 없으면 서비스를 시작하지 않으며, client ID를
지정한 재실행에서 SSM으로 설정을 반영한 뒤 서비스를 활성화합니다.
수동 절차에서는 Phase 11에서 CloudFront domain을 얻은 다음 이 Phase로 돌아와
OAuth 설정과 서비스를 활성화합니다.

```ini
[Unit]
Description=Hermes Agent Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
Group=ec2-user
WorkingDirectory=/home/ec2-user
Environment="HOME=/home/ec2-user"
Environment="HERMES_HOME=/home/ec2-user/.hermes"
Environment="AWS_REGION=us-west-2"
Environment="AWS_DEFAULT_REGION=us-west-2"
Environment="PATH=/home/ec2-user/.local/bin:/home/ec2-user/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/ec2-user/.local/bin/hermes dashboard --host 0.0.0.0 --port 9119 --no-open
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-dashboard.service
sudo systemctl status hermes-dashboard.service
curl -I http://127.0.0.1:9119/
```

## Phase 10: ALB

ALB는 서로 다른 AZ의 private subnet 두 개에 internal로 배치하고 EC2
`9119`를 target으로 등록합니다.

```bash
ALB_ARN=$(aws elbv2 create-load-balancer \
  --name "alb-${PROJECT}" \
  --subnets "$PRIVATE_SUBNET_A" "$PRIVATE_SUBNET_B" \
  --security-groups "$ALB_SG_ID" \
  --scheme internal \
  --type application \
  --query 'LoadBalancers[0].LoadBalancerArn' \
  --output text)

TG_ARN=$(aws elbv2 create-target-group \
  --name "tg-${PROJECT}" \
  --protocol HTTP \
  --port 9119 \
  --vpc-id "$VPC_ID" \
  --target-type instance \
  --health-check-path / \
  --matcher HttpCode=200-399 \
  --query 'TargetGroups[0].TargetGroupArn' \
  --output text)

aws elbv2 register-targets \
  --target-group-arn "$TG_ARN" \
  --targets "Id=${INSTANCE_ID},Port=9119"
```

CloudFront가 VPC origin으로 연결하므로 listener는 HTTP `80` 하나이며 별도의
인증서가 필요하지 않습니다.

```bash
aws elbv2 create-listener \
  --load-balancer-arn "$ALB_ARN" \
  --protocol HTTP \
  --port 80 \
  --default-actions \
    'Type=fixed-response,FixedResponseConfig={StatusCode=403,ContentType=text/plain,MessageBody=Access-denied}'
```

다음 보호도 함께 적용합니다.

1. ALB Security Group source를 CloudFront origin-facing managed prefix list의
   TCP `80`으로 제한합니다.
2. listener 기본 action은 `403` fixed response로 둡니다.
3. 임의의 custom origin header가 일치할 때만 forward하는 listener rule을
   만듭니다.

자동 installer가 이 세 항목을 모두 구성합니다. 자세한 확인은
[alb-setup.md](./alb-setup.md)를 참고하세요.

## Phase 11: CloudFront

먼저 internal ALB를 대상으로 VPC origin을 생성합니다.

```bash
VPC_ORIGIN_ID=$(aws cloudfront create-vpc-origin \
  --vpc-origin-endpoint-config \
    "Name=${PROJECT}-alb-vpc-origin,Arn=${ALB_ARN},HTTPPort=80,HTTPSPort=443,OriginProtocolPolicy=http-only" \
  --query 'VpcOrigin.Id' \
  --output text)

aws cloudfront get-vpc-origin \
  --id "$VPC_ORIGIN_ID" \
  --query 'VpcOrigin.Status'
```

`Status`가 `Deployed`가 된 뒤 distribution의 origin에 VPC origin ID를
지정하고 다음 behavior를 사용합니다.

- Viewer protocol: redirect HTTP to HTTPS
- Origin domain: internal ALB DNS 이름
- Origin: `VpcOriginConfig.VpcOriginId=${VPC_ORIGIN_ID}`
- Allowed methods: 모든 method
- Cache policy: `CachingDisabled`
- Origin request policy: `AllViewerExceptHostHeader`
- custom origin header: ALB listener rule과 같은 secret
- `X-Forwarded-Proto: https`: OAuth secure cookie와 callback scheme 전달

CloudFront 생성 JSON은 header secret을 포함하므로 repository에 저장하지
마세요. 자동 installer는 distribution을 생성하고 ALB protection과 같은
secret을 state에 보관합니다.

상태 확인:

```bash
aws cloudfront get-distribution \
  --id <DISTRIBUTION_ID> \
  --query 'Distribution.{Status:Status,Domain:DomainName}' \
  --output table

aws cloudfront wait distribution-deployed \
  --id <DISTRIBUTION_ID>
```

접속:

```text
https://<CLOUDFRONT_DOMAIN>/
```

Dashboard 로그인은 Nous Portal OAuth가 처리합니다. 자동 배포의 client ID와
callback URL은 `assets/hermes-deployment-info.md`에서 확인합니다. 상세 운영은
[cloudfront-setup.md](./cloudfront-setup.md)를 참고하세요.

## Phase 12: Telegram messaging gateway

Telegram을 사용할 때만 EC2에서 추가로 설정합니다.

```bash
sudo su - ec2-user
hermes gateway setup
```

마법사에서 Telegram, BotFather token, DM pairing을 선택합니다. foreground
확인 후 service로 설치합니다.

```bash
hermes gateway run -v
# Ctrl+C

hermes gateway install
hermes gateway start
hermes gateway status --deep
```

사용자가 받은 pairing code를 승인합니다.

```bash
hermes pairing list
hermes pairing approve telegram <CODE>
```

gateway는 Telegram API를 polling하므로 inbound port나 ALB rule이 필요하지
않습니다. NAT Gateway를 통한 HTTPS outbound가 필요합니다. 자세한 내용은
[telegram-setup.md](./telegram-setup.md)를 참고하세요.

## Phase 13: Bedrock Knowledge Base와 retrieve skill

자동 installer는 기본적으로 다음 resource를 생성합니다.

1. versioning과 encryption이 활성화된 S3 bucket
2. Bedrock Knowledge Base service role
3. OpenSearch Serverless vector collection과 index
4. S3 `docs/` prefix를 사용하는 data source
5. Bedrock Knowledge Base
6. Bedrock Agent Runtime VPC Endpoint
7. EC2의 `~/.hermes/skills/retrieve`
8. EC2의 `~/.hermes/knowledge-base.json`

OpenSearch Serverless의 encryption, network, data access policy와 Bedrock
service role 간 ARN이 정확히 연결돼야 하므로 수동 생성보다 installer 사용을
권장합니다. 전체 구현은 `installer.py`의 Knowledge Base section이
기준입니다.

정상 상태의 OpenSearch network policy는 public access와 Dashboard를 허용하지
않고 `bedrock.amazonaws.com`만 source service로 허용합니다. data access
policy도 Knowledge Base service role의 index 읽기·쓰기만 유지합니다.
installer가 로컬에서 vector index를 생성하거나 확인하는 동안에만 collection
endpoint와 배포자 principal의 index 관리 권한을 임시로 추가하고, 성공 여부와
관계없이 private policy와 service role 전용 권한으로 복원합니다.

로컬 `contents/` 문서를 업로드하고 ingestion합니다.

```bash
python3 add_content.py
```

```bash
python3 add_content.py --delete-missing
python3 add_content.py --force-sync
python3 add_content.py --no-wait
```

EC2에서 검색:

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "운영 장애 복구 순서"
```

retrieve script는 EC2 Instance Profile을 사용하며 access key를 요구하지
않습니다.

## Phase 14: 최종 검증

### AWS resource

```bash
aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].{State:State.Name,PrivateIp:PrivateIpAddress,PublicIp:PublicIpAddress}'

aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=${INSTANCE_ID}"

aws elbv2 describe-target-health \
  --target-group-arn "$TG_ARN"
```

EC2에 public IP가 없어야 하고, SSM PingStatus와 ALB target이 정상이어야
합니다.

### Hermes와 Dashboard

```bash
sudo su - ec2-user
hermes --version
hermes config check
hermes doctor
hermes dashboard --status
exit

sudo systemctl status hermes-dashboard.service
sudo ss -ltnp | grep 9119
```

### Bedrock

```bash
sudo -u ec2-user -H bash -lc \
  'hermes chat -q "연결 확인이라고 답해줘"'
```

### Telegram

```bash
sudo -u ec2-user -H bash -lc \
  'hermes gateway status --deep && hermes pairing list'
```

### Knowledge Base

```bash
sudo -u ec2-user -H bash -lc \
  'python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py "테스트"'
```

## 보안 Best Practices

### 네트워크

- EC2는 private subnet에 두고 public IP를 할당하지 않습니다.
- SSH ingress를 열지 않고 SSM Session Manager를 사용합니다.
- EC2 `9119`는 ALB Security Group에서만 허용합니다.
- ALB는 CloudFront prefix list와 custom origin header로 보호합니다.
- Bedrock Runtime과 Agent Runtime은 private VPC Endpoint를 사용합니다.

### 인증과 secret

- EC2는 Instance Profile을 사용하고 static AWS key를 저장하지 않습니다.
- Dashboard OAuth 등록을 계정별로 관리하고 origin header와 bot token을
  공유하지 않습니다.
- Telegram은 DM pairing 또는 숫자 user ID allowlist를 사용합니다.
- secret을 SSM command parameter나 shell history에 넣지 않습니다.

### 데이터

- EBS와 Knowledge Base S3 bucket을 encryption합니다.
- S3 public access를 차단하고 versioning을 사용합니다.
- presigned URL은 짧은 만료 시간을 사용합니다.
- CloudTrail, ALB/CloudFront log와 운영 log의 보존 기준을 정합니다.

### Agent 권한

- terminal command와 file write approval 정책을 검토합니다.
- 외부 skill은 `hermes skills inspect`와 `hermes skills audit` 후 설치합니다.
- EC2 IAM policy를 실제 model, Knowledge Base와 bucket으로 제한합니다.

## 트래픽 흐름

### Dashboard

```text
Browser
  -> CloudFront HTTPS
  -> CloudFront VPC Origin ENI
  -> Internal ALB HTTP + secret header
  -> EC2:9119
  -> Hermes Dashboard
  -> Bedrock Runtime VPC Endpoint
```

### Telegram

```text
Telegram Bot API
  <-> Internet Gateway / NAT Gateway
  <-> Hermes messaging gateway on private EC2
  -> Bedrock Runtime VPC Endpoint
```

### Retrieve

```text
Hermes retrieve skill
  -> Bedrock Agent Runtime VPC Endpoint
  -> Bedrock Knowledge Base
  -> OpenSearch Serverless
  -> S3 source document
```

## 월간 비용 고려사항

주요 고정 또는 준고정 비용 항목:

- EC2와 EBS
- NAT Gateway 시간 및 처리 data
- ALB 시간 및 LCU
- Interface VPC Endpoint 시간 및 data
- OpenSearch Serverless OCU

사용량 기반 항목:

- Bedrock model inference
- Knowledge Base ingestion와 retrieval
- CloudFront request/data transfer
- S3 request/storage
- CloudWatch log

특히 OpenSearch Serverless와 NAT Gateway는 사용량이 적어도 비용이 발생할 수
있습니다. RAG가 필요 없으면 `--disable-knowledge-base`를 사용합니다.
CloudFront를 끄면 공개 Dashboard 로그인이 HTTP로 노출되므로 installer는 해당
구성을 지원하지 않습니다.

## 유지보수

### Hermes 업데이트

```bash
sudo su - ec2-user
hermes update
hermes --version
hermes doctor
exit

sudo systemctl restart hermes-dashboard.service
```

messaging gateway를 설치했다면 별도로 재시작합니다.

```bash
sudo -u ec2-user -H bash -lc \
  'hermes gateway restart && hermes gateway status --deep'
```

### 로그

```bash
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
sudo tail -200 /var/log/hermes-install.log

sudo -u ec2-user -H bash -lc 'hermes logs --since 1h'
sudo -u ec2-user -H bash -lc 'hermes gateway status --full'
```

### Knowledge Base 갱신

```bash
python3 add_content.py
```

문서를 삭제한 결과까지 반영하려면:

```bash
python3 add_content.py --delete-missing
```

## 트러블슈팅

### Hermes 설치 실패

```bash
sudo tail -200 /var/log/hermes-install.log
sudo -u ec2-user -H bash -lc 'command -v hermes; hermes --version'
```

private subnet의 NAT route, DNS와 HTTPS outbound를 확인합니다.

### Dashboard 502

```bash
sudo systemctl status hermes-dashboard.service
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
sudo ss -ltnp | grep 9119
curl -I http://127.0.0.1:9119/
```

그다음 Target Group health와 EC2 Security Group source를 확인합니다.

### CloudFront 403

CloudFront URL에서 발생했다면 distribution status, custom origin header와 ALB
listener rule을 확인합니다. origin FQDN 직접 호출의 `403`은 기본 보안
동작입니다.

### Dashboard 로그인 실패

Nous Portal 계정으로 로그인하며, Portal의 self-hosted dashboard 등록에 현재
CloudFront callback URL이 설정되어 있는지 확인합니다. Telegram bot token을
입력하지 않습니다.

### Bedrock AccessDenied

```bash
aws sts get-caller-identity
hermes config show
hermes doctor
```

EC2 Instance Profile, model access, model ID, Region 및
`bedrock:InvokeModel*` 권한을 확인합니다.

### Telegram이 응답하지 않음

```bash
hermes gateway status --deep
hermes pairing list
curl -I https://api.telegram.org
```

Dashboard service 상태가 아니라 messaging gateway와 NAT outbound를
확인합니다.

### Retrieve 실패

```bash
test -f ~/.hermes/knowledge-base.json
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py "테스트"
```

Knowledge Base status, ingestion status, Agent Runtime endpoint,
`bedrock:Retrieve` 및 S3 `GetObject` 권한을 확인합니다.

## 인프라 삭제

자동 installer로 생성한 resource는 state와 관리 태그를 검증하는 uninstaller로
삭제합니다.

```bash
python3 uninstaller.py
```

CloudFront, VPC origin과 NAT Gateway 삭제는 완료까지 시간이 걸릴 수
있습니다. VPC origin의 ENI가 정리되기 전에는 subnet과 VPC 삭제가 실패하므로
uninstaller가 순서대로 대기합니다. 수동 `create-instance.sh`로 만든 EC2는
별도 관리 태그를 사용하므로 자동 uninstaller 대상이 아닙니다.

## 참고 링크

- [Hermes Agent GitHub](https://github.com/NousResearch/hermes-agent)
- [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs/)
- [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)
- [Amazon Bedrock](https://docs.aws.amazon.com/bedrock/)
- [Amazon Bedrock Knowledge Bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [Knowledge Base 보안 구성](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-create-security.html)
- [S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [Elastic Load Balancing](https://docs.aws.amazon.com/elasticloadbalancing/)
- [Amazon CloudFront](https://docs.aws.amazon.com/cloudfront/)

## 주요 문서

- [README](./README.md)
- [ALB 설정](./alb-setup.md)
- [CloudFront 설정](./cloudfront-setup.md)
- [Telegram 설정](./telegram-setup.md)
- [Hermes 사용 명령](./use_command.md)
- [Retrieve skill](./skills/retrieve/SKILL.md)
