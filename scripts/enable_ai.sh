#!/usr/bin/env bash
# Turn on AI email reading for the TMS on Cloud Run (see docs/ai-email.md).
# Run in Cloud Shell from the repo root:  bash scripts/enable_ai.sh [model-name]
set -euo pipefail
PROJECT=${PROJECT:-atsstms}
REGION=${REGION:-us-central1}
CANDIDATES=("${1:-}" gemini-3.8-flash gemini-3.7-flash gemini-3.6-flash gemini-3.5-flash gemini-2.5-flash)

gcloud config set project "$PROJECT" >/dev/null 2>&1
NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')

echo "1/4 启用 Vertex AI…"
gcloud services enable aiplatform.googleapis.com

echo "2/4 寻找可用的 Gemini 模型…"
TOKEN=$(gcloud auth print-access-token)
MODEL=""
for m in "${CANDIDATES[@]}"; do
  [ -z "$m" ] && continue
  RESP=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/global/publishers/google/models/$m:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"Reply with the single word OK"}]}]}')
  if grep -q '"candidates"' <<<"$RESP"; then MODEL=$m; echo "   可用：$m"; break; fi
  echo "   $m 不可用：$(grep -o '"message": *"[^"]*"' <<<"$RESP" | head -1)"
done
if [ -z "$MODEL" ]; then
  echo "没有找到可用的模型。到 Vertex AI → Model Garden 查 Gemini Flash 的模型名，然后运行：bash scripts/enable_ai.sh 模型名"
  exit 1
fi

echo "3/4 允许网站调用 Gemini…"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" \
  --role=roles/aiplatform.user --condition=None --quiet >/dev/null

echo "4/4 部署新版本并开启 AI 识别（约 3–5 分钟）…"
gcloud run deploy tms --source . --region "$REGION" --quiet \
  --update-env-vars "TMS_AI_ENABLED=1,TMS_AI_MODEL=$MODEL"

echo
echo "完成。打开 https://tms-$NUMBER.$REGION.run.app/ai/ 开始使用。"
