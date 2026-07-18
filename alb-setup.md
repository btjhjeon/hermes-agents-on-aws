# Hermes Agent ALB 외부 접속 설정 가이드

Hermes Dashboard를 private subnet의 EC2에 유지하면서 외부에서 접속하기 위한
ALB 구성입니다. ALB의 대상은 messaging gateway가 아니라
`hermes-dashboard.service`의 포트 `9119`입니다.

## 아키텍처

```text
CloudFront
  -> CloudFront VPC Origin (VPC 내부 ENI)
  -> Internal ALB:80 (private subnets)
  -> EC2:9119 (private subnet)
  -> Hermes Dashboard
```

Telegram, Discord 등의 messaging gateway는 외부에서 들어오는 ALB 연결을
사용하지 않습니다. polling 방식 플랫폼은 NAT Gateway를 통해 외부 API에
연결합니다.

## 배포 정보 확인

installer가 생성한 실제 값은 `assets/hermes-deployment.json`과
`assets/hermes-deployment-info.md`에 있습니다.

```bash
jq '{
  region,
  vpc_id,
  public_subnets,
  private_subnets,
  instance_id,
  private_ip,
  alb_dns_name,
  vpc_origin_id,
  target_group_arn,
  cloudfront_enabled
}' assets/hermes-deployment.json
```

직접 명령을 실행할 때 사용할 값을 변수로 가져올 수 있습니다.

```bash
STATE=assets/hermes-deployment.json
REGION=$(jq -r .region "$STATE")
INSTANCE_ID=$(jq -r .instance_id "$STATE")
ALB_DNS=$(jq -r .alb_dns_name "$STATE")
TG_ARN=$(jq -r .target_group_arn "$STATE")
```

## 접속 URL

ALB는 private subnet의 internal ALB이므로 인터넷에서 직접 접근할 수
없습니다. CloudFront가 VPC origin을 통해 전달하는 요청 중 installer가
생성한 secret origin header가 있는 것만 Target Group으로 전달되고, 그 외에는
`403`을 반환합니다. 사용자는 `https://<CLOUDFRONT_DOMAIN>/`으로 접속합니다.

CloudFront와 ALB 사이 구간은 VPC를 벗어나지 않으므로 listener는 HTTP `80`
하나이며, viewer와 CloudFront 사이는 CloudFront 기본 인증서의 HTTPS로
보호됩니다. Dashboard 로그인은 Nous Portal OAuth를 사용합니다.

## ALB 구성

installer는 다음 리소스를 자동으로 구성합니다.

| 항목 | 설정 |
|---|---|
| ALB | Internal, private subnet 2개 이상 |
| Listener | HTTP `80` (VPC 내부 트래픽 전용) |
| CloudFront VPC Origin | ALB ARN 대상, `http-only` |
| Target Group | HTTP `9119`, target type `instance` |
| Health Check | `GET /`, success `200-399` |
| EC2 Security Group | ALB Security Group에서 오는 `9119`만 허용 |
| ALB Security Group | CloudFront origin-facing prefix list의 TCP `80`만 허용 |

CloudFront가 활성화되면 ALB listener의 기본 action은 `403`이고, 올바른 custom
header를 가진 요청만 Target Group으로 전달됩니다. header 값은 배포 상태에
저장되지만 인증정보이므로 출력하거나 공유하지 마세요.

## 상태 확인

```bash
aws elbv2 describe-load-balancers \
  --load-balancer-arns "$(jq -r .alb_arn "$STATE")" \
  --region "$REGION"

aws elbv2 describe-target-health \
  --target-group-arn "$TG_ARN" \
  --region "$REGION"
```

EC2 내부에서 Dashboard를 확인합니다.

```bash
aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids "$INSTANCE_ID" \
  --parameters \
    'commands=["systemctl status hermes-dashboard.service --no-pager","ss -ltnp | grep 9119","curl -I http://127.0.0.1:9119/"]' \
  --region "$REGION"
```

명령 결과는 다음과 같이 확인합니다.

```bash
aws ssm get-command-invocation \
  --command-id <COMMAND_ID> \
  --instance-id "$INSTANCE_ID" \
  --region "$REGION"
```

## 수동 구성 시 주의사항

수동으로 EC2와 ALB를 구성하는 경우 Dashboard를 모든 interface에서 실행해야
합니다.

```ini
[Service]
User=ec2-user
Environment="HOME=/home/ec2-user"
Environment="HERMES_HOME=/home/ec2-user/.hermes"
ExecStart=/home/ec2-user/.local/bin/hermes dashboard \
  --host 0.0.0.0 --port 9119 --no-open
```

설정 후 다음 명령으로 반영합니다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-dashboard.service
```

Hermes Dashboard의 listen address는 실행 인자로 지정하고, 인증은
`~/.hermes/config.yaml`의 Nous OAuth 설정을 사용합니다.

```yaml
dashboard:
  oauth:
    client_id: agent:<ID>
  public_url: https://<CLOUDFRONT_DOMAIN>
```

## 트러블슈팅

### 502 Bad Gateway

Target이 unhealthy이거나 Dashboard가 실행되지 않는 경우입니다.

```bash
sudo systemctl status hermes-dashboard.service
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
sudo ss -ltnp | grep 9119
curl -I http://127.0.0.1:9119/
```

설치가 끝나지 않았다면 user data 로그를 확인합니다.

```bash
sudo tail -200 /var/log/hermes-install.log
```

### Target Unhealthy

다음 항목을 확인합니다.

1. EC2가 private subnet에서 실행 중인지 확인합니다.
2. EC2 Security Group이 ALB Security Group에서 오는 TCP `9119`를 허용하는지
   확인합니다.
3. Target Group에 올바른 instance ID가 등록됐는지 확인합니다.
4. Health Check path가 `/`, success code가 `200-399`인지 확인합니다.
5. Dashboard가 `0.0.0.0:9119`에서 listen하는지 확인합니다.

### ALB DNS 이름 직접 접속 불가

internal ALB이므로 인터넷에서 ALB DNS 이름을 열 수 없는 것이 정상입니다.
CloudFront URL로 접속하세요. VPC 내부(예: SSM으로 접속한 EC2)에서 origin
header 없이 `curl`하면 `403`이 반환되는 것도 정상입니다.

### 로그인 실패

Nous Portal의 self-hosted dashboard client ID, callback URL과 EC2 설정을
확인합니다.

```bash
sed -n '/## Dashboard/,$p' assets/hermes-deployment-info.md
sudo -u ec2-user -H hermes config show
```

callback URL은 `https://<CLOUDFRONT_DOMAIN>/auth/callback`이어야 합니다.

## WhatsApp 연동

WhatsApp은 ALB 설정이 아니라 Hermes messaging gateway에서 구성합니다.

```bash
sudo su - ec2-user
hermes gateway setup
hermes gateway install
hermes gateway start
hermes gateway status --deep
```

설정 마법사에서 WhatsApp을 선택하고 안내되는 인증 절차를 따릅니다. Dashboard
접속은 상태 확인과 설정 관리에 편리하지만 WhatsApp 메시지 자체가 ALB를
통과하는 것은 아닙니다.

## 보안 고려사항

- 기본 구성처럼 EC2는 private subnet에 두고 SSH 인바운드를 열지 않습니다.
- ALB는 internal로 유지해 인터넷에 노출하지 않습니다.
- ALB listener의 custom origin header 검증을 유지합니다.
- 공개 Dashboard에는 Nous OAuth를 사용하고 origin header를 secret으로
  취급합니다.
- WAF가 필요하면 CloudFront distribution에 연결합니다.

## 비용

ALB는 시간 및 LCU 사용량에 따라 과금됩니다. NAT Gateway, EC2, CloudFront,
Bedrock, OpenSearch Serverless 비용은 별도입니다. 실제 가격은 배포 Region의
AWS Pricing을 확인하세요.

## 참고

- [Elastic Load Balancing 문서](https://docs.aws.amazon.com/elasticloadbalancing/)
- [CloudFront VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
- [Hermes Agent 문서](https://hermes-agent.nousresearch.com/docs/)
- [CloudFront 설정](./cloudfront-setup.md)
