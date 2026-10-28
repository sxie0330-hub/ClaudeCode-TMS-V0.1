#!/usr/bin/env bash
# Turn on the daily to-do e-mail (see docs/notifications.md).
# Run in Cloud Shell from the repo root:  bash scripts/enable_email.sh
set -euo pipefail
PROJECT=${PROJECT:-atsstms}
REGION=${REGION:-us-central1}
JOB=tms-daily-digest
SCHEDULE=${SCHEDULE:-"0 7 * * 1-6"}   # 7:00 Pacific, Monday–Saturday

gcloud config set project "$PROJECT" >/dev/null 2>&1
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SITE="https://tms-$NUMBER.$REGION.run.app"

read -r -p "发件邮箱（建议用专门的邮箱，例如 tms@advtransolution.com）: " SENDER
read -r -s -p "这个邮箱的 16 位应用专用密码（输入时不显示）: " APPPW; echo
APPPW=${APPPW// /}
if [ ${#APPPW} -ne 16 ]; then echo "应用专用密码应该是 16 位字母，你输入了 ${#APPPW} 位，请重新运行。"; exit 1; fi

put_secret() {  # name value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --data-file=- >/dev/null
  fi
}

echo "1/4 启用 Cloud Scheduler，保存密码…"
gcloud services enable cloudscheduler.googleapis.com
put_secret tms-email-password "$APPPW"
if ! KEY=$(gcloud secrets versions access latest --secret=tms-task-key 2>/dev/null); then
  KEY=$(openssl rand -hex 24)
  put_secret tms-task-key "$KEY"
fi

echo "2/4 部署新版本并开启邮件（约 3–5 分钟）…"
gcloud run deploy tms --source . --region "$REGION" --quiet \
  --update-env-vars "TMS_EMAIL_USER=$SENDER,TMS_SITE_URL=$SITE" \
  --update-secrets "TMS_EMAIL_PASSWORD=tms-email-password:latest,TMS_TASK_KEY=tms-task-key:latest"

echo "3/4 设置每天定时发送（$SCHEDULE，太平洋时间）…"
if gcloud scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB" --location "$REGION" --schedule "$SCHEDULE" --time-zone "America/Los_Angeles" \
    --uri "$SITE/tasks/daily-digest/" --http-method POST --update-headers "X-TMS-Task-Key=$KEY" --attempt-deadline 300s >/dev/null
else
  gcloud scheduler jobs create http "$JOB" --location "$REGION" --schedule "$SCHEDULE" --time-zone "America/Los_Angeles" \
    --uri "$SITE/tasks/daily-digest/" --http-method POST --headers "X-TMS-Task-Key=$KEY" --attempt-deadline 300s >/dev/null
fi

echo "4/4 测试：用同样的方式调用一次…"
curl -s -X POST -H "X-TMS-Task-Key: $KEY" "$SITE/tasks/daily-digest/"; echo
echo
echo "完成。上面一行显示 {\"sent\": …} 就是成功（sent = 发出的封数；没有待办的人不会收到）。"
echo "以后每天 7:00（周一到周六）自动发送。每个人可以在 今日待办 → 规则设置 里关掉或发测试邮件。"
