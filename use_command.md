# Hermes Agent 사용 가이드

이 문서는 이 저장소로 AWS에 설치한 Hermes Agent의 CLI, Dashboard, messaging
gateway, skill, cron 및 원격 운영 명령을 정리합니다.

## 주요 실행 방식

Hermes에는 서로 독립적인 세 가지 실행 방식이 있습니다.

| 방식 | 명령/서비스 | 용도 |
|---|---|---|
| Terminal | `hermes` | EC2 또는 개인 PC의 대화형 agent |
| Dashboard | `hermes-dashboard.service` | 포트 `9119`의 Web UI |
| Messaging gateway | `hermes gateway ...` | Telegram, Discord, WhatsApp 등 |

Dashboard를 재시작해도 Telegram gateway는 재시작되지 않으며, 그 반대도
마찬가지입니다.

## 기본 명령어

```bash
# 버전
hermes --version

# 전체 도움말
hermes --help

# 대화 시작
hermes

# TUI를 명시적으로 사용
hermes --tui

# 단일 질문
hermes chat -q "이 디렉터리의 구조를 설명해줘"

# 최근 session 이어서 시작
hermes -c

# 진단
hermes doctor
```

대화 중에는 다음 slash command를 사용할 수 있습니다.

```text
/new
/reset
/model
/skills
/compress
/usage
/insights
/retry
/undo
```

## 설정 관리

```bash
# 현재 설정
hermes config show

# 설정 경로
hermes config path

# 편집기로 열기
hermes config edit

# 단일 값 설정
hermes config set model.default <MODEL_ID>

# 누락되거나 오래된 설정 검사
hermes config check

# 지원되는 migration 확인/실행
hermes config migrate
```

secret key를 `hermes config set`으로 설정하면 Hermes가 secret 저장소인
`~/.hermes/.env`로 routing할 수 있습니다. 명령줄에 token을 적으면 shell
history에 남을 수 있으므로 provider나 messaging platform 설정은 가능한 한
`hermes setup`, `hermes model`, `hermes gateway setup` 마법사를 사용하세요.

### AWS Bedrock 설정

installer가 생성하는 기본 설정은 다음 구조입니다.

```yaml
model:
  default: global.anthropic.claude-sonnet-4-5-20250929-v1:0
  provider: bedrock
  base_url: https://bedrock-runtime.us-west-2.amazonaws.com
bedrock:
  region: us-west-2
```

```bash
# 대화형 model 선택
hermes model

# model ID 직접 변경
hermes config set model.default <BEDROCK_MODEL_ID>
hermes config check
```

EC2에서는 Instance Profile이 credential을 제공합니다. access key를
`config.yaml`이나 `.env`에 추가하지 않습니다.

## Session과 로그

```bash
hermes sessions list
hermes sessions browse
hermes logs
hermes logs -f
hermes logs errors
hermes logs --since 1h
```

session 관련 상세 명령은 설치된 버전의 도움말을 기준으로 확인합니다.

```bash
hermes sessions --help
```

## Skill 관리

```bash
# 설치된 skill
hermes skills list

# registry 검색과 미리 보기
hermes skills search "pdf"
hermes skills inspect <IDENTIFIER>

# 설치
hermes skills install <IDENTIFIER>

# update 확인 및 적용
hermes skills check
hermes skills update

# 보안 audit
hermes skills audit
hermes skills audit --deep
```

외부 skill은 내용을 `inspect`하고 audit 결과를 확인한 뒤 설치하세요. skill은
agent에 tool 사용 절차를 제공하므로 신뢰하지 않는 script가 포함될 수
있습니다.

### Retrieve skill

Knowledge Base가 활성화된 배포에는 retrieve skill이 자동 설치됩니다.

```bash
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py \
  "에러 코드 1001의 원인"
```

채팅에서는 다음과 같이 요청할 수 있습니다.

```text
/retrieve 에러 코드 1001의 원인과 해결 절차를 찾아줘
```

결과에는 passage, relevance score, 문서 제목, page, S3 URI와 1시간 동안
유효한 presigned URL이 포함됩니다.

## Browser tool

AWS installer는 기본적으로 Hermes browser tool이 사용할 Chromium과 Linux
library를 설치합니다. browser는 별도 daemon 명령으로 관리하지 않고 agent가
tool call로 실행합니다.

설정은 Hermes setup과 현재 설정에서 확인합니다.

```bash
hermes setup
hermes config show
hermes doctor
```

installer에 `--skip-browser`를 사용했다면 browser 관련 package가 설치되지
않습니다.

## Cron

Cron scheduler는 지속 실행 중인 messaging gateway가 처리합니다.

```bash
# 생성: 30분마다 실행
hermes cron create "30m" \
  "서비스 상태를 점검하고 이상이 있을 때만 알려줘" \
  --name service-check \
  --deliver telegram

# 생성: 매일 09:00
hermes cron create "0 9 * * *" \
  "오늘의 운영 요약을 작성해줘" \
  --name daily-summary \
  --deliver telegram

hermes cron list
hermes cron list --all
hermes cron status
hermes cron run <JOB_ID>
hermes cron pause <JOB_ID>
hermes cron resume <JOB_ID>
hermes cron remove <JOB_ID>
```

`run`은 다음 scheduler tick에 실행되도록 요청합니다. delivery 대상과 script
mode 등 추가 옵션은 도움말에서 확인합니다.

```bash
hermes cron create --help
```

## Dashboard 운영

AWS installer는 다음 명령과 같은 Dashboard를 systemd로 자동 실행합니다.

```bash
hermes dashboard --host 0.0.0.0 --port 9119 --no-open
```

서비스를 운영할 때는 systemd를 사용합니다.

```bash
sudo systemctl status hermes-dashboard.service
sudo systemctl restart hermes-dashboard.service
sudo journalctl -u hermes-dashboard.service -f
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
```

Hermes 자체의 process 탐지 결과도 확인할 수 있습니다.

```bash
hermes dashboard --status
```

접속 URL과 Nous OAuth callback/client ID 상태는
`assets/hermes-deployment-info.md`에 있습니다. 공개 Dashboard는 Nous Portal
계정으로 로그인합니다.

### SSM port forwarding

ALB를 거치지 않고 진단하려면 EC2의 `9119`를 local port로 forwarding할 수
있습니다.

```bash
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9119"],"localPortNumber":["9119"]}' \
  --region us-west-2
```

이 forwarding은 서비스 응답 진단용입니다. OAuth callback은
`dashboard.public_url`의 CloudFront 주소로 돌아가므로 일반 로그인은 배포
정보의 HTTPS URL에서 진행합니다.

## Messaging gateway 관리

### 설정

```bash
hermes gateway setup
```

Telegram 등 각 platform의 token, DM 정책과 allowlist를 설정합니다.

### 실행과 service

```bash
# foreground
hermes gateway run -v

# user service 설치
hermes gateway install

# lifecycle
hermes gateway start
hermes gateway stop
hermes gateway restart

# 상태
hermes gateway status
hermes gateway status --deep
hermes gateway status --full
hermes gateway list
```

Linux boot-time system service가 필요하면 system scope로 설치할 수 있습니다.
user scope와 동시에 실행하지 마세요.

```bash
sudo /home/ec2-user/.local/bin/hermes gateway install \
  --system \
  --run-as-user ec2-user \
  --start-now \
  --start-on-login
```

### Pairing

```bash
hermes pairing list
hermes pairing approve telegram <CODE>
hermes pairing revoke telegram <USER_ID>
hermes pairing clear-pending
```

## SSM을 통한 원격 관리

### Session 접속

```bash
aws ssm start-session \
  --target <INSTANCE_ID> \
  --region us-west-2

sudo su - ec2-user
```

### 비대화형 명령 실행

Hermes의 home, PATH와 login environment를 사용하도록 `ec2-user`의 login
shell에서 실행합니다.

```bash
COMMAND_ID=$(aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids <INSTANCE_ID> \
  --parameters \
    'commands=["sudo -u ec2-user -H bash -lc '\''hermes --version && hermes doctor'\''"]' \
  --region us-west-2 \
  --query 'Command.CommandId' \
  --output text)

aws ssm get-command-invocation \
  --command-id "$COMMAND_ID" \
  --instance-id <INSTANCE_ID> \
  --region us-west-2
```

Dashboard service는 system scope이므로 root 명령으로 관리합니다.

```bash
aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids <INSTANCE_ID> \
  --parameters \
    'commands=["systemctl restart hermes-dashboard.service","systemctl status hermes-dashboard.service --no-pager"]' \
  --region us-west-2
```

Bot token이나 password를 SSM command parameter에 넣지 마세요. command
history와 AWS API audit log에 남을 수 있습니다.

## 실제 사용 예시

### 코드와 파일 작업

```text
이 저장소의 테스트 실패 원인을 조사하고 최소한의 수정으로 해결해줘.
변경한 파일과 실행한 검증을 마지막에 요약해줘.
```

Hermes가 terminal과 file tool을 사용할 때 command approval prompt를
확인하고, production credential이나 민감한 경로를 작업 directory에 두지
마세요.

### Knowledge Base 질의

```text
/retrieve 업로드된 운영 가이드에서 장애 복구 순서를 찾아 출처와 함께 요약해줘.
```

### Telegram 예약 보고

```bash
hermes cron create "0 8 * * 1-5" \
  "오늘 확인해야 할 운영 항목을 요약해줘" \
  --name weekday-ops \
  --deliver telegram
```

## 업데이트

```bash
hermes update
hermes --version
hermes doctor
```

Dashboard와 gateway는 별도로 재시작합니다.

```bash
sudo systemctl restart hermes-dashboard.service
hermes gateway restart
```

## 트러블슈팅

### Hermes 명령을 찾을 수 없음

```bash
sudo su - ec2-user
command -v hermes
echo "$PATH"
ls -l ~/.local/bin/hermes
```

SSM의 `ssm-user`가 아니라 `ec2-user` environment에서 실행해야 합니다.

### Dashboard 접속 실패

```bash
sudo systemctl status hermes-dashboard.service
sudo journalctl -u hermes-dashboard.service -n 100 --no-pager
sudo ss -ltnp | grep 9119
curl -I http://127.0.0.1:9119/
```

그다음 ALB Target Health와 CloudFront 배포 상태를 확인합니다.

### Telegram 연결 실패

```bash
hermes gateway status --deep
hermes pairing list
hermes doctor
curl -I https://api.telegram.org
```

Dashboard status가 정상이어도 messaging gateway는 중지돼 있을 수 있습니다.

### Bedrock 연결 실패

```bash
aws sts get-caller-identity
aws bedrock list-foundation-models \
  --region us-west-2 \
  --query 'modelSummaries[].modelId'
hermes config show
hermes doctor
```

확인할 항목:

- model ID가 해당 Region에서 지원되는지
- 계정에서 model access가 활성화됐는지
- EC2 Instance Profile의 Bedrock 권한
- `bedrock.region`과 endpoint Region이 같은지
- Bedrock Runtime VPC Endpoint와 Security Group

### Retrieve 실패

```bash
test -f ~/.hermes/knowledge-base.json
python3 ~/.hermes/skills/retrieve/scripts/retrieve_search.py "테스트"
```

Knowledge Base가 `ACTIVE`이고 ingestion이 완료됐는지, EC2 Role에
`bedrock:Retrieve`와 해당 S3 object의 `s3:GetObject` 권한이 있는지
확인합니다.

## 참고 링크

- [Hermes Agent 문서](https://hermes-agent.nousresearch.com/docs/)
- [Hermes CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- [Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [GitHub](https://github.com/NousResearch/hermes-agent)
