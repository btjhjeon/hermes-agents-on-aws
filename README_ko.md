# AWS에서 Hermes Agent 안전하게 활용하기

[English](./README.md) | 한국어

Hermes Agent를 AWS에 독립적으로 배포하는 저장소입니다. 기존 환경을
재사용하지 않고 전용 VPC, private subnet의 EC2, IAM Role, Bedrock VPC
Endpoint, NAT Gateway, ALB, CloudFront를 생성합니다. 웹에서는 Hermes
Dashboard를 사용하고, 필요하면 별도의 messaging gateway를 구성해 Telegram
등에서 대화할 수 있습니다.

```text
Browser
  -> CloudFront (HTTPS)
  -> CloudFront VPC Origin (VPC 내부 ENI)
  -> Internal ALB (CloudFront origin 검증)
  -> EC2 private IP:9119
  -> Hermes Dashboard

Telegram
  <-> Hermes messaging gateway on EC2
  -> NAT Gateway
  -> Telegram Bot API

Hermes Agent
  -> Bedrock Runtime VPC Endpoint
  -> Amazon Bedrock
  -> retrieve skill
  -> Bedrock Knowledge Base / S3 / OpenSearch Serverless
```

ALB와 CloudFront는 Dashboard를 안전하게 외부에 제공하기 위한 구성입니다.
ALB는 private subnet의 internal ALB이며, CloudFront가 VPC origin으로 직접
연결하므로 인터넷에 노출되지 않습니다. Telegram polling에는 ALB가 필요하지
않지만, EC2의 인터넷 아웃바운드를 위한 NAT Gateway가 필요합니다.

## 사전 준비

- Python 3.10 이상
- AWS CLI에 설정된 배포 권한
- 사용할 Region에서 활성화된 Amazon Bedrock 모델 접근 권한
- 공개 Dashboard 로그인에 사용할 Nous Portal 계정
- `boto3` 설치

```bash
python3 -m pip install -r requirements.txt
aws sts get-caller-identity
```

## AWS에 설치

공개 Dashboard는 Hermes 공식 권장 방식인 Nous OAuth를 사용합니다. CloudFront
callback URL이 배포 중 생성되므로 설치는 두 단계로 진행됩니다.

먼저 인프라를 생성합니다. 이 단계에서는 인증되지 않은 Dashboard가 노출되지
않도록 서비스가 중지된 상태로 유지됩니다.

```bash
python3 installer.py
```

installer는 CloudFront VPC origin을 생성해 private subnet의 internal ALB로
직접 연결합니다. 별도의 domain이나 인증서 없이 사용자 접속 URL은 기본
CloudFront domain입니다.

`assets/hermes-deployment-info.md`에 기록된 OAuth callback URL을
[Nous Portal Local Dashboards](https://portal.nousresearch.com/local-dashboards)에
등록하고 `agent:...` 형식의 client ID를 발급받습니다. 그다음 같은 옵션에
client ID를 추가해 재실행합니다.

```bash
python3 installer.py \
  --dashboard-oauth-client-id agent:<ID>
```

installer는 SSM을 통해 OAuth 설정을 반영하고 Dashboard를 시작합니다. 기존
username/password 배포에 같은 명령을 실행하면 basic-auth 설정을 제거하고
OAuth로 전환합니다.

CloudFront domain은 distribution을 재생성할 때마다 바뀝니다. 재설치 후 기존
client ID를 재사용하면 로그인 시 `redirect_uri_mismatch`가 발생하므로, Nous
Portal 등록의 URL을 새 callback URL로 수정해야 합니다. 자세한 절차는
[CloudFront 가이드](./cloudfront-setup.md#로그인-시-redirect_uri_mismatch)를
참고하세요.

기본값은 `us-west-2`, `t3.medium`, CloudFront 및 Knowledge Base 활성화입니다.
주요 옵션은 다음과 같습니다.

```bash
python3 installer.py \
  --region us-west-2 \
  --instance-type t3.medium \
  --model-id global.anthropic.claude-sonnet-5
```

```text
--dashboard-oauth-client-id ID   Nous Portal OAuth client ID (agent:...)
--disable-knowledge-base        Knowledge Base 관련 리소스 생략
--skip-browser                  Hermes browser tool용 Chromium 설치 생략
```

`--disable-cloudfront`는 Dashboard 로그인을 public HTTP로 노출하므로 지원하지
않습니다.

설치 결과는 다음 파일에 저장됩니다.

- `assets/hermes-deployment.json`: 재실행 및 삭제에 사용하는 배포 상태
- `assets/hermes-deployment-info.md`: 접속 URL, OAuth callback, 운영 명령

상태 파일에는 origin 검증 secret이 포함될 수 있으므로 외부에 공유하지 마세요.
같은 옵션으로 installer를 다시 실행하면 저장된 상태를 기준으로 리소스를
재사용하거나 복구합니다.

## Dashboard 접속

OAuth 설정을 완료한 뒤 `assets/hermes-deployment-info.md`의 URL을 브라우저에서
열고 **Sign in with Nous Research**를 선택합니다. Hermes Dashboard는
installer가 생성한 `hermes-dashboard.service`가 포트 `9119`에서 제공합니다.

```bash
aws ssm start-session \
  --target <INSTANCE_ID> \
  --region us-west-2

sudo systemctl status hermes-dashboard.service
sudo journalctl -u hermes-dashboard.service -f
```

ALB는 internal이므로 인터넷에서 직접 접근할 수 없고, VPC origin을 통해
CloudFront가 전달한 요청 중 origin 검증 header가 일치하는 것만 EC2로
전달합니다. 상세 설정은 [ALB 가이드](./alb-setup.md)와
[CloudFront 가이드](./cloudfront-setup.md)를 참고하세요.

## Telegram 연결

Dashboard와 Telegram gateway는 서로 다른 프로세스입니다. installer는
Dashboard만 자동 실행하므로 Telegram을 사용하려면 EC2에서 Hermes 공식
gateway 설정을 한 번 수행해야 합니다.

```bash
aws ssm start-session \
  --target <INSTANCE_ID> \
  --region us-west-2

sudo su - ec2-user
hermes gateway setup
hermes gateway install
hermes gateway start
hermes gateway status --deep
```

설정 마법사에서 Telegram과 BotFather가 발급한 token을 선택합니다. DM
pairing을 사용하면 사용자가 받은 code를 다음과 같이 승인합니다.

```bash
hermes pairing list
hermes pairing approve telegram <CODE>
```

token을 명령줄이나 문서에 직접 기록하지 마세요. Hermes는 secret을
`~/.hermes/.env`에 저장합니다. 전체 절차와 장애 진단은
[Telegram 가이드](./telegram-setup.md)를 참고하세요.

## EC2에서 Hermes 사용

SSM으로 접속한 최초 shell은 `ssm-user`이므로 Hermes를 설치한 `ec2-user`로
전환합니다.

```bash
sudo su - ec2-user
hermes --version
hermes doctor
hermes config show
hermes dashboard --status
hermes gateway status
```

터미널 대화는 다음 명령으로 시작합니다.

```bash
hermes
```

한 번만 질문하려면 다음과 같이 실행합니다.

```bash
hermes chat -q "현재 프로젝트를 요약해줘"
```

더 많은 명령은 [사용 가이드](./use_command.md)에 정리되어 있습니다.

## 개인 PC에 설치

공식 installer를 사용합니다.

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup
```

이 저장소의 AWS installer는 EC2 Instance Profile을 사용하므로 AWS access key를
인스턴스에 저장하지 않습니다. 개인 PC에서 Bedrock을 사용할 때는 AWS CLI
profile이나 SSO 등 표준 credential chain을 별도로 구성해야 합니다.

## Bedrock 모델 설정

AWS 배포 시 installer가 `~/.hermes/config.yaml`에 다음 구조를 생성합니다.

```yaml
model:
  default: global.anthropic.claude-sonnet-5
  provider: bedrock
  base_url: https://bedrock-runtime.us-west-2.amazonaws.com
bedrock:
  region: us-west-2
```

모델을 변경할 때는 Region에서 사용할 수 있고 계정에 접근 권한이 있는 Bedrock
model 또는 inference profile ID를 지정합니다.

```bash
hermes model
hermes config set model.default <BEDROCK_MODEL_ID>
hermes config check
```

installer가 관리하는 EC2에서는 Instance Profile이 인증을 제공하므로
`AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`를 설정하지 않습니다.

## Skill

설치된 skill을 확인하고 registry에서 검색하거나 설치할 수 있습니다.

```bash
hermes skills list
hermes skills search "document"
hermes skills inspect <IDENTIFIER>
hermes skills install <IDENTIFIER>
hermes skills audit
```

Knowledge Base가 활성화되면 installer가 retrieve skill을
`~/.hermes/skills/retrieve`에 배치합니다. 문서 검색은 채팅에서 `/retrieve
<질문>`으로 요청하거나 EC2에서 직접 확인할 수 있습니다.

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "장애 코드의 원인과 해결 방법"
```

## Cron

Gateway가 계속 실행 중이어야 예약 작업이 처리됩니다.

```bash
hermes cron create "0 9 * * *" \
  "오늘의 운영 상태를 요약해줘" \
  --name daily-status \
  --deliver telegram

hermes cron list
hermes cron status
hermes cron run <JOB_ID>
hermes cron pause <JOB_ID>
hermes cron resume <JOB_ID>
hermes cron remove <JOB_ID>
```

## Kiro CLI 설치

Kiro CLI를 함께 사용하려면 EC2에 SSM으로 접속한 뒤 `ec2-user`로 전환합니다.

```bash
sudo su - ec2-user
curl -fsSL https://cli.kiro.dev/install | bash
kiro-cli login --use-device-flow
kiro-cli chat
```

Kiro CLI 인증과 과금은 Hermes 및 이 저장소가 생성한 IAM Role과 별개입니다.

## Gmail 연결

Hermes의 Google Workspace 관련 skill이 요구하는 도구와 OAuth credential을
준비해야 합니다. `gog`를 사용하는 경우 로컬 PC에서는 다음과 같이 설치하고
인증할 수 있습니다.

```bash
brew install steipete/tap/gogcli
gog auth credentials /path/to/client_secret.json
gog auth add your-email@gmail.com --services gmail,calendar,drive,contacts
gog auth list
```

Google Cloud Console에서 Gmail, Calendar, Drive API를 활성화하고 Desktop app
유형의 OAuth client를 사용합니다. headless EC2에서 OAuth callback을 처리해야
한다면 Dashboard의 터미널이나 안내되는 device flow를 사용하고, credential
파일을 저장소에 commit하지 마세요.

## Knowledge Base에 문서 등록

installer는 기본적으로 S3, OpenSearch Serverless, Bedrock Knowledge Base와
retrieve skill을 함께 생성합니다. `contents/` 아래에 지원 문서를 넣고 다음을
실행합니다.

```bash
python3 add_content.py
```

지원 형식은 PDF, TXT, Markdown, HTML, CSV, DOC/DOCX, XLS/XLSX입니다. 파일의
SHA-256이 바뀐 경우에만 다시 업로드하고, 변경이 있으면 ingestion이 완료될
때까지 기다립니다.

```bash
# S3에서 로컬에 없는 문서도 삭제
python3 add_content.py --delete-missing

# 문서 변경이 없어도 ingestion 시작
python3 add_content.py --force-sync

# ingestion 시작 후 기다리지 않고 종료
python3 add_content.py --no-wait
```

`contents/error_code.pdf`처럼 기존에 사용하던 문서도 같은 디렉터리에 두면
그대로 인덱싱됩니다. 배포 식별자는 하드코딩하지 않고
`assets/hermes-deployment.json`에서 읽습니다.

## 업데이트

Hermes 공식 updater를 사용한 뒤 서비스를 재시작합니다.

```bash
sudo su - ec2-user
hermes update
exit

sudo systemctl restart hermes-dashboard.service
```

Telegram gateway를 설치했다면 `ec2-user`에서 별도로 재시작합니다.

```bash
sudo su - ec2-user
hermes gateway restart
hermes gateway status --deep
```

## 인프라 삭제

installer가 기록한 `assets/hermes-deployment.json`을 기준으로 관리 대상
리소스만 삭제합니다. CloudFront distribution과 VPC origin을 삭제한 뒤
ALB, VPC 순으로 정리합니다.

```bash
python3 uninstaller.py
```

수동 `create-instance.sh`로 만든 인스턴스는 별도 태그를 사용하므로 이
uninstaller의 관리 대상이 아닙니다.

## 상세 문서

- [AWS 수동 배포 가이드](./hermes-aws-deploy.md)
- [ALB 설정](./alb-setup.md)
- [CloudFront 설정](./cloudfront-setup.md)
- [Telegram 설정](./telegram-setup.md)
- [Hermes 명령어와 운영](./use_command.md)
- [Retrieve skill](./skills/retrieve/SKILL.md)

## Reference

- [Hermes Agent GitHub](https://github.com/NousResearch/hermes-agent)
- [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs/)
- [Amazon Bedrock Knowledge Bases](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html)
- [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)
