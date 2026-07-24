# Hermes Agent Telegram Bot 설정 가이드

Hermes의 messaging gateway를 Telegram Bot API와 polling 방식으로 연결합니다.
Dashboard와 messaging gateway는 별도 프로세스이므로
`hermes-dashboard.service`를 재시작해도 Telegram gateway가 시작되지는
않습니다.

## 사전 준비

1. Telegram에서 [@BotFather](https://t.me/BotFather)를 엽니다.
2. `/newbot`을 실행합니다.
3. bot 이름과 `_bot`으로 끝나는 username을 정합니다.
4. BotFather가 발급한 token을 안전하게 보관합니다.

token은 bot 계정 전체 권한을 가지므로 문서, shell script, Git 또는 채팅에
남기지 마세요.

## EC2 접속

installer 출력의 instance ID와 Region을 사용합니다.

```bash
STATE=assets/hermes-deployment.json
REGION=$(jq -r .region "$STATE")
INSTANCE_ID=$(jq -r .instance_id "$STATE")

aws ssm start-session \
  --target "$INSTANCE_ID" \
  --region "$REGION"
```

Hermes는 `ec2-user`의 home에 설치되어 있습니다.

```bash
sudo su - ec2-user
hermes --version
```

## Telegram 구성

공식 설정 마법사를 실행합니다.

```bash
hermes gateway setup
```

1. messaging platform으로 Telegram을 선택합니다.
2. BotFather token을 입력합니다.
3. DM 접근 정책은 pairing을 권장합니다.
4. 필요한 경우 허용할 Telegram user ID를 설정합니다.

Hermes는 Telegram token을 `~/.hermes/.env`의
`TELEGRAM_BOT_TOKEN`으로 저장합니다. token 값을 확인하거나 출력할 필요는
없습니다.

## Gateway 실행

먼저 foreground에서 연결을 확인할 수 있습니다.

```bash
hermes gateway run -v
```

정상 동작을 확인한 뒤 `Ctrl+C`로 종료하고 background service로 설치합니다.

```bash
hermes gateway install
hermes gateway start
hermes gateway status --deep
```

Linux server에서 로그인 여부와 관계없이 boot 시 system service로 실행해야
한다면 Hermes가 제공하는 system scope를 사용할 수 있습니다.

```bash
sudo /home/ec2-user/.local/bin/hermes gateway install \
  --system \
  --run-as-user ec2-user \
  --start-now \
  --start-on-login

sudo /home/ec2-user/.local/bin/hermes gateway status --system --deep
```

user scope와 system scope를 동시에 실행하지 마세요. 현재 설치 scope는
`hermes gateway status --deep`과 `hermes gateway list`로 확인합니다.

## 사용 방법

Telegram에서 bot username을 열고 메시지를 보냅니다. pairing 정책을
선택했다면 bot이 승인 code를 반환합니다. EC2의 `ec2-user` shell에서
승인합니다.

```bash
hermes pairing list
hermes pairing approve telegram <CODE>
```

승인된 사용자를 철회하려면 다음 명령을 사용합니다.

```bash
hermes pairing revoke telegram <USER_ID>
```

Telegram 대화에서도 Hermes 공통 slash command를 사용할 수 있습니다.

```text
/new
/model
/skills
/usage
/status
```

## 설정 변경

platform, token 또는 DM 정책을 변경할 때는 설정 파일을 직접 편집하기보다
마법사를 다시 실행합니다.

```bash
hermes gateway setup
hermes gateway restart
hermes gateway status --deep
```

secret 이외의 현재 설정은 다음 명령으로 확인합니다.

```bash
hermes config show
hermes config check
hermes config path
```

`hermes config show`의 출력을 공유하기 전에도 secret이나 개인 식별자가 없는지
확인하세요.

## DM 보안

### Pairing

알 수 없는 사용자가 메시지를 보내면 일회성 code를 받고 관리자가
`hermes pairing approve telegram <CODE>`로 승인합니다. 공개 bot에 권장되는
기본 방식입니다.

### Allowlist

미리 지정한 Telegram user ID만 허용합니다. username은 변경될 수 있으므로
가능하면 숫자 user ID를 사용합니다. 설정 마법사에서 허용 대상을 지정하세요.

### Open access

누구나 agent를 호출할 수 있어 Bedrock 비용과 tool 실행 권한이 노출됩니다.
테스트 이외에는 사용하지 않는 것이 좋습니다.

## 상태와 로그

```bash
hermes gateway status
hermes gateway status --deep
hermes gateway status --full
hermes gateway list
```

사용자 service의 상태는 Hermes가 출력하는 unit 이름으로 확인합니다.
profile에 따라 `hermes-gateway-<profile>.service`처럼 이름이 달라질 수 있으므로
고정된 unit 이름을 가정하지 마세요.

```bash
systemctl --user list-units 'hermes-gateway*'
journalctl --user -u '<UNIT_NAME>' -n 100 --no-pager
```

system scope로 설치했다면 system journal을 사용합니다.

```bash
sudo systemctl list-units 'hermes-gateway*'
sudo journalctl -u '<UNIT_NAME>' -n 100 --no-pager
```

## 트러블슈팅

### Bot이 응답하지 않음

```bash
hermes doctor
hermes gateway status --deep
hermes config check
```

다음 항목을 확인합니다.

- gateway process가 실제로 실행 중인지
- BotFather에서 받은 token이 현재 bot의 token인지
- 같은 token으로 다른 polling process가 동시에 실행 중이지 않은지
- EC2가 NAT Gateway를 통해 `api.telegram.org:443`에 연결할 수 있는지
- Bedrock model access와 EC2 IAM Role이 정상인지

연결 확인:

```bash
curl -I https://api.telegram.org
```

### Unauthorized

token이 잘못됐거나 BotFather에서 폐기된 경우입니다. BotFather에서 새 token을
발급하고 `hermes gateway setup`을 다시 실행한 뒤 gateway를 재시작합니다.

```bash
hermes gateway setup
hermes gateway restart
```

이전 token은 즉시 폐기합니다.

### Pairing code가 보이지 않음

```bash
hermes pairing list
hermes gateway status --deep
```

pending 목록이 비어 있으면 사용자가 해당 bot에 새 메시지를 보냈는지,
gateway 설정에서 pairing을 선택했는지 확인합니다.

### 재부팅 후 중지됨

user service가 login session에 묶여 있을 수 있습니다. `hermes gateway
status`로 scope를 확인한 뒤 system scope 설치를 사용하거나 `ec2-user`의
linger 정책을 운영 기준에 맞게 설정합니다. user service와 system service를
중복 실행하지 마세요.

### Dashboard는 되지만 Telegram은 안 됨

정상적으로 발생할 수 있는 독립적인 상태입니다. Dashboard는 ALB/CloudFront를
통해 들어오는 웹 서비스이고, Telegram은 별도 gateway가 NAT를 통해 polling
합니다. `hermes-dashboard.service`가 아닌 `hermes gateway status --deep`을
확인하세요.

## 보안 권장사항

- DM pairing 또는 숫자 user ID allowlist를 사용합니다.
- token을 Git, SSM command parameter, shell history에 남기지 않습니다.
- EC2 Instance Profile에는 필요한 AWS 권한만 부여합니다.
- Hermes terminal tool의 write/command approval 정책을 검토합니다.
- agent가 접근할 수 있는 local file과 skill을 최소화합니다.
- 의심스러운 접근이 있으면 pairing 승인을 철회하고 bot token을 회전합니다.

## 참고

- [Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)
- [Hermes Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- [Telegram Bot API](https://core.telegram.org/bots/api)
