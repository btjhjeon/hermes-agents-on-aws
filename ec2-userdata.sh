#!/bin/bash
set -euo pipefail

# Hermes Agent EC2 User Data Script
# Amazon Linux 2023 + Hermes Agent + Amazon Bedrock
# Architecture: CloudFront -> ALB -> EC2 (Private Subnet)
# 아래 값은 직접 실행할 때 수정할 수 있으며 create-instance.sh는 환경 변수로
# region, model, OAuth 설정, browser 설치 여부를 주입합니다.

AWS_REGION="${AWS_REGION:-us-west-2}"
HERMES_MODEL_ID="${HERMES_MODEL_ID:-global.anthropic.claude-sonnet-4-5-20250929-v1:0}"
DASHBOARD_OAUTH_CLIENT_ID="${DASHBOARD_OAUTH_CLIENT_ID:-}"
DASHBOARD_PUBLIC_URL="${DASHBOARD_PUBLIC_URL:-}"
DASHBOARD_PUBLIC_URL="${DASHBOARD_PUBLIC_URL%/}"
INSTALL_BROWSER="${INSTALL_BROWSER:-true}"

exec > >(tee /var/log/hermes-install.log)
exec 2>&1

echo "=== Hermes Agent installation started ==="

SYSTEM_PACKAGES=(curl git python3)
HERMES_INSTALL_OPTIONS=(--non-interactive --skip-setup)

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

case "$INSTALL_BROWSER" in
  true)
    SYSTEM_PACKAGES+=(
      alsa-lib
      at-spi2-atk
      at-spi2-core
      atk
      cairo
      cups-libs
      libdrm
      libxkbcommon
      mesa-libgbm
      nss
      pango
    )
    ;;
  false)
    HERMES_INSTALL_OPTIONS+=(--skip-browser)
    ;;
  *)
    echo "ERROR: INSTALL_BROWSER는 true 또는 false여야 합니다."
    exit 1
    ;;
esac

dnf install -y "${SYSTEM_PACKAGES[@]}"
timedatectl set-timezone Asia/Seoul || true

INSTALL_FLAGS="${HERMES_INSTALL_OPTIONS[*]}"
sudo -u ec2-user -H bash -lc \
  "set -o pipefail; curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- $INSTALL_FLAGS"

export AWS_REGION
export HERMES_MODEL_ID
export DASHBOARD_OAUTH_CLIENT_ID
export DASHBOARD_PUBLIC_URL

install -d -m 700 -o ec2-user -g ec2-user /home/ec2-user/.hermes

python3 <<'PY'
import json
import os
from pathlib import Path

config = {
    "model": {
        "default": os.environ["HERMES_MODEL_ID"],
        "provider": "bedrock",
        "base_url": (
            f"https://bedrock-runtime.{os.environ['AWS_REGION']}.amazonaws.com"
        ),
    },
    "bedrock": {"region": os.environ["AWS_REGION"]},
    "dashboard": {
        "oauth": {
            "client_id": os.environ.get("DASHBOARD_OAUTH_CLIENT_ID", ""),
        },
    },
}
if os.environ.get("DASHBOARD_PUBLIC_URL"):
    config["dashboard"]["public_url"] = os.environ["DASHBOARD_PUBLIC_URL"]

home = Path("/home/ec2-user/.hermes")
(home / "config.yaml").write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

chown ec2-user:ec2-user /home/ec2-user/.hermes/config.yaml
chmod 600 /home/ec2-user/.hermes/config.yaml

cat > /etc/systemd/system/hermes-dashboard.service <<'SERVICE'
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
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-dashboard

[Install]
WantedBy=multi-user.target
SERVICE

sed -i \
  "s/AWS_REGION=us-west-2/AWS_REGION=$AWS_REGION/; s/AWS_DEFAULT_REGION=us-west-2/AWS_DEFAULT_REGION=$AWS_REGION/" \
  /etc/systemd/system/hermes-dashboard.service

systemctl daemon-reload
if [ -n "$DASHBOARD_OAUTH_CLIENT_ID" ]; then
  systemctl enable --now hermes-dashboard.service
else
  systemctl disable --now hermes-dashboard.service || true
fi

echo "=== Hermes Agent installation completed ==="
if [ -n "$DASHBOARD_OAUTH_CLIENT_ID" ]; then
  echo "Dashboard: $DASHBOARD_PUBLIC_URL (Nous OAuth)"
else
  echo "Dashboard: OAuth registration pending; service is disabled"
fi
