#!/usr/bin/env bash
# One-time: let GitHub deploy the TMS to Cloud Run after every merge to main.
# No keys are created or stored: GitHub proves who it is to Google Cloud
# (Workload Identity Federation), and only this repository's main branch is accepted.
# Run in Cloud Shell:  cd ~/tms && git pull && bash scripts/enable_auto_deploy.sh
set -euo pipefail
PROJECT=${PROJECT:-atsstms}
REPO=${REPO:-sxie0330-hub/ClaudeCode-TMS-V0.1}
POOL=github
SA_NAME=github-deployer

gcloud config set project "$PROJECT" >/dev/null 2>&1
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
RUNTIME_SA="$NUMBER-compute@developer.gserviceaccount.com"

echo "1/4 启用所需服务…"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com >/dev/null

echo "2/4 建立部署专用账号并授权…"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="GitHub auto deploy (TMS)" >/dev/null
for ROLE in roles/run.admin roles/cloudbuild.builds.editor roles/storage.admin roles/artifactregistry.writer \
            roles/serviceusage.serviceUsageConsumer roles/viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$ROLE" \
    --condition=None --quiet >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --member="serviceAccount:$SA" \
  --role=roles/iam.serviceAccountUser --quiet >/dev/null

echo "3/4 只信任 GitHub 上 $REPO 的 main 分支…"
gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --location=global --display-name="GitHub Actions" >/dev/null
COND="assertion.repository=='$REPO' && assertion.ref=='refs/heads/main'"
if gcloud iam workload-identity-pools providers describe github --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc github --location=global --workload-identity-pool="$POOL" \
    --attribute-condition="$COND" >/dev/null
else
  gcloud iam workload-identity-pools providers create-oidc github --location=global --workload-identity-pool="$POOL" \
    --display-name="GitHub" --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="$COND" >/dev/null
fi
gcloud iam service-accounts add-iam-policy-binding "$SA" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO" \
  --quiet >/dev/null

echo "4/4 完成。"
echo "以后代码合并到 main，GitHub 会自动跑测试并部署（约 5–8 分钟），不用再手动运行部署命令。"
echo "进度和结果：https://github.com/$REPO/actions"
