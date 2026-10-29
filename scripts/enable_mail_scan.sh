#!/usr/bin/env bash
# Turn on the daily mailbox scan (see docs/mail-scan.md).
# Run in Cloud Shell from the repo root:  bash scripts/enable_mail_scan.sh
set -euo pipefail
PROJECT=${PROJECT:-atsstms}
REGION=${REGION:-us-central1}
JOB=tms-scan-email
SCHEDULE=${SCHEDULE:-"0 17 * * *"}   # every day 5:00 pm Los Angeles time (PDT/PST)

gcloud config set project "$PROJECT" >/dev/null 2>&1
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SITE="https://tms-$NUMBER.$REGION.run.app"

echo "1/4 启用 Gmail API 和 Cloud Scheduler…"
gcloud services enable gmail.googleapis.com cloudscheduler.googleapis.com

if ! KEY=$(gcloud secrets versions access latest --secret=tms-task-key 2>/dev/null); then
  KEY=$(openssl rand -hex 24)
  printf %s "$KEY" | gcloud secrets create tms-task-key --data-file=- >/dev/null
fi

echo "2/4 部署新版本（约 3–5 分钟）…"
gcloud run deploy tms --source . --region "$REGION" --quiet --timeout 600 \
  --update-secrets "TMS_TASK_KEY=tms-task-key:latest"

echo "3/4 设置每天定时扫描（$SCHEDULE，洛杉矶时间）…"
if gcloud scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB" --location "$REGION" --schedule "$SCHEDULE" --time-zone "America/Los_Angeles" \
    --uri "$SITE/tasks/scan-email/" --http-method POST --update-headers "X-TMS-Task-Key=$KEY" --attempt-deadline 600s >/dev/null
else
  gcloud scheduler jobs create http "$JOB" --location "$REGION" --schedule "$SCHEDULE" --time-zone "America/Los_Angeles" \
    --uri "$SITE/tasks/scan-email/" --http-method POST --headers "X-TMS-Task-Key=$KEY" --attempt-deadline 600s >/dev/null
fi

echo "4/4 完成。"
echo
echo "还差一步（在网页上做，只做一次）："
echo "  Google Cloud 控制台 → Google Auth Platform → Clients → TMS 客户端 → Authorized redirect URIs → Add URI："
echo "  $SITE/mail/callback/"
echo "  保存后等几分钟，再打开 $SITE/mail/ 点「连接我的邮箱」。"
