# Hermes Agent CloudFront 배포 가이드

CloudFront에서 HTTPS를 종료하고 VPC origin을 통해 private subnet의 internal
ALB로 연결해 Hermes Dashboard를 제공합니다.

## 아키텍처

```text
Browser
  -> HTTPS
CloudFront
  -> VPC Origin + secret origin header
CloudFront VPC Origin ENI (VPC 내부)
  -> Internal ALB:80
  -> HTTP:9119
EC2 private subnet
  -> hermes-dashboard.service
```

Dashboard 로그인은 공개 배포에 적합한 Hermes Nous OAuth provider가 처리합니다.
CloudFront 자체 token이나 messaging platform의 bot token은 사용하지 않습니다.

## VPC Origin

CloudFront VPC origin은 CloudFront가 VPC 내부에 관리형 ENI를 만들어 internal
ALB에 직접 연결하는 기능입니다. ALB가 인터넷에 노출되지 않으므로 별도의
domain, Route 53 hosted zone, ACM 인증서가 필요하지 않습니다.

```bash
python3 installer.py
```

CloudFront와 ALB 사이 구간은 VPC를 벗어나지 않으며, 브라우저의 접속 URL은
`https://<CLOUDFRONT_DOMAIN>/`입니다. VPC origin 생성과 삭제에는 각각 몇 분이
소요될 수 있습니다.

## 배포 정보

실제 resource ID와 URL은 installer가 생성한 파일에서 확인합니다.

```bash
jq '{
  region,
  distribution_id,
  domain_name,
  vpc_origin_id,
  alb_dns_name,
  target_group_arn,
  instance_id
}' assets/hermes-deployment.json
```

```bash
STATE=assets/hermes-deployment.json
REGION=$(jq -r .region "$STATE")
DIST_ID=$(jq -r .distribution_id "$STATE")
CF_DOMAIN=$(jq -r .domain_name "$STATE")
INSTANCE_ID=$(jq -r .instance_id "$STATE")
TG_ARN=$(jq -r .target_group_arn "$STATE")
```

## 접속 URL

```text
https://<CLOUDFRONT_DOMAIN>/
```

브라우저에서 **Sign in with Nous Research**를 선택합니다. OAuth client ID와
callback URL은 `assets/hermes-deployment-info.md`에서 확인할 수 있습니다.
internal ALB는 인터넷에서 직접 접근할 수 없습니다.

## 설치되는 설정

installer는 다음 CloudFront behavior를 사용합니다.

| 항목 | 설정 |
|---|---|
| Viewer protocol | HTTP를 HTTPS로 redirect |
| Origin | VPC origin, internal ALB port `80` (`http-only`) |
| Allowed methods | GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE |
| Cache policy | Managed CachingDisabled |
| Origin request policy | AllViewerExceptHostHeader |
| Compression | 활성 |
| HTTP version | HTTP/2 및 HTTP/3 |
| Origin protection | 임의의 custom header |
| Forwarded scheme | `X-Forwarded-Proto: https` |

Dashboard는 동적 API와 streaming 응답을 포함하므로 cache를 비활성화합니다.
origin header는 CloudFront가 자동으로 추가하고 ALB listener rule이 검증합니다.
그 값은 secret으로 취급합니다.

ALB Security Group은 AWS managed prefix list
`com.amazonaws.global.cloudfront.origin-facing`에서 오는 TCP `80`만
허용합니다. prefix list를 찾지 못하면 installer는 전체 인터넷으로
fallback하지 않고 배포를 중단합니다.

## 배포 상태 확인

```bash
aws cloudfront get-distribution \
  --id "$DIST_ID" \
  --query 'Distribution.{Status:Status,Domain:DomainName,Enabled:DistributionConfig.Enabled}' \
  --output table
```

`Status`가 `Deployed`가 될 때까지 기다립니다.

```bash
aws cloudfront wait distribution-deployed --id "$DIST_ID"
```

전체 경로를 확인합니다.

```bash
curl -I "https://$CF_DOMAIN/"

aws elbv2 describe-target-health \
  --target-group-arn "$TG_ARN" \
  --region "$REGION"
```

## Dashboard 인증

첫 installer 실행은 CloudFront domain을 만든 뒤 Dashboard를 중지된 상태로
둡니다. 배포 정보의 callback URL을
https://portal.nousresearch.com/local-dashboards 에 등록하고 발급받은
`agent:...` client ID로 installer를 재실행합니다.

```bash
python3 installer.py \
  --dashboard-oauth-client-id agent:<ID>
```

installer는 다음 OAuth 설정을 SSM으로 반영하고 서비스를 시작합니다.

```yaml
dashboard:
  oauth:
    client_id: agent:<ID>
  public_url: https://<CLOUDFRONT_DOMAIN>
```

Nous Portal의 redirect URI는
`https://<CLOUDFRONT_DOMAIN>/auth/callback`과 정확히 일치해야 합니다.

## 캐시 무효화

기본 cache policy가 disabled이므로 일반적인 Dashboard 사용에서 invalidation은
필요하지 않습니다. 정적 파일 갱신을 즉시 확인해야 할 때만 실행합니다.

```bash
aws cloudfront create-invalidation \
  --distribution-id "$DIST_ID" \
  --paths "/*"
```

진행 상태는 다음과 같이 확인합니다.

```bash
aws cloudfront list-invalidations \
  --distribution-id "$DIST_ID"
```

## 트러블슈팅

### CloudFront 502

ALB Target Group과 Dashboard를 순서대로 확인합니다.

```bash
aws elbv2 describe-target-health \
  --target-group-arn "$TG_ARN" \
  --region "$REGION"

aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids "$INSTANCE_ID" \
  --parameters \
    'commands=["systemctl status hermes-dashboard.service --no-pager","ss -ltnp | grep 9119","curl -I http://127.0.0.1:9119/"]' \
  --region "$REGION"
```

서비스 로그도 확인합니다.

```bash
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
sudo tail -200 /var/log/hermes-install.log
```

### CloudFront 403

가능한 원인은 다음과 같습니다.

- distribution 또는 VPC origin이 아직 `Deployed` 상태가 아님
- CloudFront origin custom header와 ALB listener rule 값이 다름
- ALB Security Group이 CloudFront origin-facing prefix list의 TCP `80`을
  허용하지 않음
- distribution이 비활성화됨

VPC origin 상태는 다음과 같이 확인합니다.

```bash
aws cloudfront get-vpc-origin \
  --id "$(jq -r .vpc_origin_id "$STATE")" \
  --query 'VpcOrigin.Status'
```

### 로그인 화면 반복

CloudFront가 cookie와 query string을 origin으로 전달하는지 확인합니다. 이
저장소의 installer는 `AllViewerExceptHostHeader` origin request policy를
사용합니다. 다음 설정이 서로 일치하는지도 확인하세요.

- `dashboard.oauth.client_id`와 Nous Portal의 self-hosted dashboard client ID
- `dashboard.public_url`과 현재 CloudFront HTTPS URL
- Nous Portal redirect URI와 `<public_url>/auth/callback`

### Streaming 또는 WebSocket 문제

cache가 비활성인지, 모든 HTTP method와 viewer header/cookie가 전달되는지
확인합니다. ALB idle timeout과 CloudFront origin read timeout보다 긴 요청은
연결이 종료될 수 있습니다. 먼저 Dashboard 서비스 로그에서 서버 측 오류를
확인하세요.

## 배포 비활성화와 삭제

전체 인프라를 삭제할 때는 state와 관리 태그를 검증하는 uninstaller를
사용하는 것이 권장됩니다. distribution을 비활성화하고 삭제한 뒤 VPC origin을
삭제합니다. VPC origin의 ENI가 남아 있으면 subnet과 VPC 삭제가 실패하므로
순서가 중요합니다.

```bash
python3 uninstaller.py
```

CloudFront만 수동으로 삭제해야 한다면 먼저 현재 ETag와 config를 가져오고
`Enabled`를 `false`로 변경한 뒤 deployed 상태를 기다려야 합니다.

```bash
aws cloudfront get-distribution-config \
  --id "$DIST_ID" \
  --output json > /tmp/hermes-cloudfront.json
```

`DistributionConfig.Enabled`를 `false`로 수정하고, 응답의 `ETag`를 사용해
update합니다. disable이 배포된 뒤 최신 ETag로 삭제합니다.

```bash
aws cloudfront delete-distribution \
  --id "$DIST_ID" \
  --if-match <LATEST_ETAG>
```

state를 남겨 둔 채 일부 resource만 수동 삭제하면 installer와 uninstaller의
재실행 시 추가 복구가 필요할 수 있습니다.

## 보안 강화

- custom origin header와 ALB의 기본 `403` action을 유지합니다.
- ALB는 internal로 유지하고 Security Group을 CloudFront managed prefix
  list로 제한합니다.
- Nous Portal에서 사용하지 않는 Dashboard OAuth 등록을 폐기합니다.
- AWS WAF가 필요하면 CloudFront distribution에 연결합니다.
- 사용자 접속 URL에 custom domain이 필요하면 별도로 ACM의 `us-east-1`
  인증서와 CloudFront alternate domain name을 설정합니다.
- CloudFront와 ALB access log를 별도 암호화 S3 bucket에 보관할 수 있습니다.

## 비용

CloudFront 요청, data transfer, invalidation 및 WAF 사용량에 따라 과금됩니다.
ALB, EC2, NAT Gateway, Bedrock, Knowledge Base와 OpenSearch Serverless 비용은
별도입니다.

## 참고

- [CloudFront 문서](https://docs.aws.amazon.com/cloudfront/)
- [CloudFront VPC origins](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)
- [CloudFront cache 정책](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/controlling-the-cache-key.html)
- [Hermes Agent 문서](https://hermes-agent.nousresearch.com/docs/)
- [ALB 설정](./alb-setup.md)
