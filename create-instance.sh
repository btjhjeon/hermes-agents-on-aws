#!/bin/bash
# Hermes Agent EC2 인스턴스 생성 스크립트 (수동)
# 전체 인프라 자동 배포는 installer.py 사용을 권장합니다.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

REGION="${REGION:-us-west-2}"
SUBNET_ID="${SUBNET_ID:-<PRIVATE_SUBNET_ID>}"
SG_ID="${SG_ID:-<EC2_SECURITY_GROUP_ID>}"
INSTANCE_PROFILE="${INSTANCE_PROFILE:-hermes-bedrock-profile}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
AMI_ID="${AMI_ID:-}"
AMI_PARAMETER="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
VOLUME_SIZE="${VOLUME_SIZE:-40}"
PROJECT_NAME="${PROJECT_NAME:-hermes-manual}"
HERMES_MODEL_ID="${HERMES_MODEL_ID:-global.anthropic.claude-sonnet-5}"
DASHBOARD_OAUTH_CLIENT_ID="${DASHBOARD_OAUTH_CLIENT_ID:-}"
DASHBOARD_PUBLIC_URL="${DASHBOARD_PUBLIC_URL:-}"
DASHBOARD_PUBLIC_URL="${DASHBOARD_PUBLIC_URL%/}"
INSTALL_BROWSER="${INSTALL_BROWSER:-true}"
KEY_NAME="${KEY_NAME:-}"
TARGET_GROUP_ARN="${TARGET_GROUP_ARN:-}"
DASHBOARD_PORT=9119
USER_DATA_FILE="${USER_DATA_FILE:-$SCRIPT_DIR/ec2-userdata.sh}"

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: AWS CLI가 필요합니다."
  exit 1
fi

if [[ "$SUBNET_ID" == \<*\> || "$SG_ID" == \<*\> ]]; then
  echo "ERROR: SUBNET_ID와 SG_ID를 환경 변수로 지정하세요."
  echo "예: SUBNET_ID=subnet-... SG_ID=sg-... ./create-instance.sh"
  exit 1
fi

if [[ ! "$VOLUME_SIZE" =~ ^[0-9]+$ ]] || (( VOLUME_SIZE < 20 )); then
  echo "ERROR: VOLUME_SIZE는 20 이상의 정수여야 합니다."
  exit 1
fi

if [[ ! "$PROJECT_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "ERROR: PROJECT_NAME은 영문, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다."
  exit 1
fi

if [[ (-n "$DASHBOARD_OAUTH_CLIENT_ID" && -z "$DASHBOARD_PUBLIC_URL") ||
      (-z "$DASHBOARD_OAUTH_CLIENT_ID" && -n "$DASHBOARD_PUBLIC_URL") ]]; then
  echo "ERROR: Dashboard OAuth client ID와 public URL은 함께 지정해야 합니다."
  exit 1
fi

if [ -n "$DASHBOARD_OAUTH_CLIENT_ID" ] &&
   [[ ! "$DASHBOARD_OAUTH_CLIENT_ID" =~ ^agent:[A-Za-z0-9._:-]+$ ]]; then
  echo "ERROR: DASHBOARD_OAUTH_CLIENT_ID는 Nous Portal의 agent:... 값이어야 합니다."
  exit 1
fi

if [ -n "$DASHBOARD_PUBLIC_URL" ] &&
   [[ ! "$DASHBOARD_PUBLIC_URL" =~ ^https://[^/[:space:]]+$ ]]; then
  echo "ERROR: DASHBOARD_PUBLIC_URL은 path 없는 public HTTPS URL이어야 합니다."
  exit 1
fi

if [ ! -f "$USER_DATA_FILE" ]; then
  echo "ERROR: $USER_DATA_FILE 파일이 없습니다."
  exit 1
fi

case "$INSTALL_BROWSER" in
  true|false) ;;
  *)
    echo "ERROR: INSTALL_BROWSER는 true 또는 false여야 합니다."
    exit 1
    ;;
esac

if [ -z "$AMI_ID" ]; then
  AMI_ID=$(aws ssm get-parameter \
    --name "$AMI_PARAMETER" \
    --region "$REGION" \
    --query 'Parameter.Value' \
    --output text)
fi

VPC_ID=$(aws ec2 describe-subnets \
  --subnet-ids "$SUBNET_ID" \
  --region "$REGION" \
  --query 'Subnets[0].VpcId' \
  --output text)

SG_VPC_ID=$(aws ec2 describe-security-groups \
  --group-ids "$SG_ID" \
  --region "$REGION" \
  --query 'SecurityGroups[0].VpcId' \
  --output text)

if [ "$VPC_ID" != "$SG_VPC_ID" ]; then
  echo "ERROR: Subnet과 Security Group의 VPC가 다릅니다."
  exit 1
fi

aws iam get-instance-profile \
  --instance-profile-name "$INSTANCE_PROFILE" \
  >/dev/null

RENDERED_USER_DATA=$(mktemp)
trap 'rm -f "$RENDERED_USER_DATA"' EXIT
{
  head -n 1 "$USER_DATA_FILE"
  printf 'export AWS_REGION=%q\n' "$REGION"
  printf 'export HERMES_MODEL_ID=%q\n' "$HERMES_MODEL_ID"
  printf 'export DASHBOARD_OAUTH_CLIENT_ID=%q\n' "$DASHBOARD_OAUTH_CLIENT_ID"
  printf 'export DASHBOARD_PUBLIC_URL=%q\n' "$DASHBOARD_PUBLIC_URL"
  printf 'export INSTALL_BROWSER=%q\n' "$INSTALL_BROWSER"
  tail -n +2 "$USER_DATA_FILE"
} > "$RENDERED_USER_DATA"

echo "=== Hermes Agent EC2 인스턴스 생성 ==="
echo "  Region   : $REGION"
echo "  VPC      : $VPC_ID"
echo "  Subnet   : $SUBNET_ID (Private)"
echo "  SG       : $SG_ID"
echo "  Profile  : $INSTANCE_PROFILE"
echo "  Type     : $INSTANCE_TYPE"
echo "  AMI      : $AMI_ID"
echo "  Volume   : ${VOLUME_SIZE}GB gp3 encrypted"
if [ -n "$DASHBOARD_OAUTH_CLIENT_ID" ]; then
  echo "  Dashboard: $DASHBOARD_PUBLIC_URL (Nous OAuth)"
else
  echo "  Dashboard: OAuth registration pending"
fi
echo "  UserData : $USER_DATA_FILE"
echo ""

RUN_ARGS=(
  --image-id "$AMI_ID"
  --instance-type "$INSTANCE_TYPE"
  --iam-instance-profile "Name=$INSTANCE_PROFILE"
  --network-interfaces
  "DeviceIndex=0,SubnetId=$SUBNET_ID,Groups=$SG_ID,AssociatePublicIpAddress=false,DeleteOnTermination=true"
  --block-device-mappings
  "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":$VOLUME_SIZE,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":true}}]"
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled"
  --tag-specifications
  "ResourceType=instance,Tags=[{Key=Name,Value=$PROJECT_NAME},{Key=ManagedBy,Value=hermes-agent-manual},{Key=Project,Value=$PROJECT_NAME}]"
  "ResourceType=volume,Tags=[{Key=Name,Value=$PROJECT_NAME-data},{Key=ManagedBy,Value=hermes-agent-manual},{Key=Project,Value=$PROJECT_NAME}]"
  --user-data "file://$RENDERED_USER_DATA"
  --region "$REGION"
  --query "Instances[0].InstanceId"
  --output text
)

if [ -n "$KEY_NAME" ]; then
  RUN_ARGS+=(--key-name "$KEY_NAME")
fi

INSTANCE_ID=$(aws ec2 run-instances "${RUN_ARGS[@]}")

echo "Instance ID: $INSTANCE_ID"
echo "인스턴스 시작 대기 중..."
aws ec2 wait instance-running \
  --instance-ids "$INSTANCE_ID" \
  --region "$REGION"

PRIVATE_IP=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --region "$REGION" \
  --query "Reservations[0].Instances[0].PrivateIpAddress" \
  --output text)

if [ -n "$TARGET_GROUP_ARN" ]; then
  aws elbv2 register-targets \
    --target-group-arn "$TARGET_GROUP_ARN" \
    --targets "Id=$INSTANCE_ID,Port=$DASHBOARD_PORT" \
    --region "$REGION"
fi

echo ""
echo "=== 인스턴스 생성 완료 ==="
echo "  Instance ID : $INSTANCE_ID"
echo "  Private IP  : $PRIVATE_IP"
echo ""
echo "SSM 접속:"
echo "  aws ssm start-session --target $INSTANCE_ID --region $REGION"
echo ""
echo "EC2에서 설치 로그 확인:"
echo "  sudo tail -150 /var/log/hermes-install.log"
echo ""
if [ -n "$DASHBOARD_OAUTH_CLIENT_ID" ]; then
  echo "Dashboard는 Nous Portal OAuth로 로그인합니다:"
  echo "  $DASHBOARD_PUBLIC_URL"
else
  echo "Dashboard 서비스는 OAuth 설정 전까지 비활성화되어 있습니다."
fi
echo ""
if [ -z "$TARGET_GROUP_ARN" ]; then
  echo "ALB를 사용한다면 Target Group에 수동 등록하세요:"
  echo "  aws elbv2 register-targets --target-group-arn <TG_ARN> --targets Id=$INSTANCE_ID,Port=$DASHBOARD_PORT --region $REGION"
fi
