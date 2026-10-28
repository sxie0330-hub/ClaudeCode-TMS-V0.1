# TMS 上线指南：Google Cloud（Cloud Run + Cloud SQL）

本指南把 TMS 部署到 Google Cloud。网站跑在 **Cloud Run**，有人访问才计费，平时在免费额度内；数据库用 **Cloud SQL for PostgreSQL** 最小档。

- **预计费用**：约 $10/月，几乎全是数据库。新账号的 $300 试用额度在 90 天内可抵扣。
- **所需时间**：第一次约 45–60 分钟，其中大约 10 分钟是等数据库创建。
- **需要准备**：
  - 一个有 Google Cloud 项目管理员权限的公司 Google 账号；
  - 一张信用卡（开通结算用）；
  - 现在 Render 上用的 `GOOGLE_CLIENT_ID` 和 `GOOGLE_CLIENT_SECRET`，在 Render → tms → Environment 里能看到。

所有命令都在 **Cloud Shell** 里运行。Cloud Shell 是 Google Cloud 网页上自带的命令行，免费，自带 `gcloud`、`git` 和 `openssl`，电脑上不用安装任何东西。

---

## 第 0 步：选项目、开结算、设预算提醒

1. 打开 <https://console.cloud.google.com>，用公司账号登录。
2. 顶部的项目选择器里选你之前建 Google 登录（OAuth 客户端）时用的项目，也就是 **ATSSTMS** 那个。用同一个项目，后面加登录回调地址最方便。
3. 左侧菜单进入 **Billing（结算）**：
   - 如果提示开通免费试用，按提示开通，就能拿到 $300 试用额度。
   - 确认这个项目已经关联到结算账号。
4. 设预算提醒：Billing → **Budgets & alerts** → Create budget，金额填 **$20/月**，提醒比例保持 50% / 90% / 100%。超出时会发邮件，但**不会自动停服**。
5. 点右上角的 **Cloud Shell 图标**（`>_`），打开命令行。

---

## 第 1 步：设置变量

在 Cloud Shell 里粘贴下面几行。第一行的 `你的项目ID` 要换成真实的 Project ID：在项目选择器里能看到，通常是小写字母加数字。

```bash
PROJECT_ID=你的项目ID
REGION=us-central1
gcloud config set project $PROJECT_ID
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
INSTANCE=$PROJECT_ID:$REGION:tms-db
SITE_HOST=tms-$PROJECT_NUMBER.$REGION.run.app
echo "网站地址将会是: https://$SITE_HOST"
```

> 地区选 `us-central1`（爱荷华），是因为费用估算用的就是这个地区的价格。从洛杉矶访问延迟大约 50 ms，感觉不出来。
>
> Cloud Shell 闲置一段时间会断开，变量也会丢。断开后重新打开，先把这一步再粘贴一遍。

---

## 第 2 步：启用需要的服务

```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

大约 1–2 分钟。

---

## 第 3 步：创建数据库（约 10 分钟）

```bash
gcloud sql instances create tms-db \
  --database-version=POSTGRES_16 --edition=enterprise --tier=db-f1-micro \
  --region=$REGION --storage-type=SSD --storage-size=10 \
  --backup-start-time=10:00 --deletion-protection
```

各参数的含义：

| 参数 | 含义 |
|---|---|
| `--edition=enterprise` | 必须写。最小档 `db-f1-micro` 只在 Enterprise 版里有 |
| `--backup-start-time=10:00` | 每天 UTC 10:00（洛杉矶凌晨 2–3 点）自动备份 |
| `--deletion-protection` | 防止误删整个数据库 |

创建完成后，建库、建用户。密码随机生成，下一步会存进 Secret Manager，**不需要你记住**：

```bash
gcloud sql databases create tms --instance=tms-db
DB_PASSWORD=$(openssl rand -hex 24)
gcloud sql users create tms --instance=tms-db --password="$DB_PASSWORD"
```

---

## 第 4 步：把密码类信息存进 Secret Manager

密码和密钥不写在网站配置里，统一放在 Secret Manager。

```bash
printf %s "$DB_PASSWORD" | gcloud secrets create tms-db-password --data-file=-
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create tms-django-secret --data-file=-
```

接着存 Google 登录的 Client Secret。运行下面这行后，**把 Secret 粘贴进去，按回车，再按 Ctrl+D**：

```bash
gcloud secrets create tms-google-secret --data-file=- 
```

> 粘贴时屏幕上会显示内容，这是正常的。

---

## 第 5 步：给网站授权

Cloud Run 用项目自带的"默认计算服务账号"运行，并用它构建代码。这个账号需要三个权限：
- 连接数据库；
- 读取 Secret；
- 构建代码（[官方文档](https://docs.cloud.google.com/run/docs/deploying-source-code)要求的 Cloud Run Builder 角色）。

```bash
SA=$PROJECT_NUMBER-compute@developer.gserviceaccount.com
for ROLE in roles/cloudsql.client roles/secretmanager.secretAccessor roles/run.builder; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member=serviceAccount:$SA --role=$ROLE --condition=None --quiet >/dev/null && echo "已授权 $ROLE"
done
```

权限生效要等一两分钟，所以授权后稍等再进行第 7 步。

---

## 第 6 步：取得代码

```bash
git clone https://github.com/sxie0330-hub/ClaudeCode-TMS-V0.1.git tms && cd tms
```

如果仓库是私有的，Git 会要求输入用户名和密码：
- **用户名**：你的 GitHub 用户名；
- **密码**：填一个 GitHub Personal Access Token，**不是** GitHub 登录密码。

Token 的生成方式：GitHub → Settings → Developer settings → Fine-grained tokens，只选这个仓库，权限给 Contents: Read-only 即可。

---

## 第 7 步：部署网站

把 `你的公司邮箱` 换成你自己的邮箱。这个邮箱第一次登录时会自动成为管理层，跟 Render 上 `TMS_BOOTSTRAP_ADMINS` 的作用一样。`你的GOOGLE_CLIENT_ID` 换成 Render 上的那个值。

```bash
gcloud run deploy tms --source . --region $REGION \
  --allow-unauthenticated \
  --add-cloudsql-instances $INSTANCE \
  --memory 512Mi --cpu 1 --max-instances 2 \
  --set-env-vars "CLOUDSQL_INSTANCE=$INSTANCE,DJANGO_ALLOWED_HOSTS=$SITE_HOST,TMS_ALLOWED_EMAIL_DOMAINS=advtransolution.com,TMS_BOOTSTRAP_ADMINS=你的公司邮箱,GOOGLE_CLIENT_ID=你的GOOGLE_CLIENT_ID" \
  --set-secrets "DB_PASSWORD=tms-db-password:latest,DJANGO_SECRET_KEY=tms-django-secret:latest,GOOGLE_CLIENT_SECRET=tms-google-secret:latest"
```

- 第一次运行会询问是否创建一个叫 `cloud-run-source-deploy` 的镜像仓库，输入 **Y**。
- 构建加部署大约 3–5 分钟。完成后会打印 `Service URL`，应该和第 1 步显示的地址一致。

几个参数的说明：

- **`--allow-unauthenticated`**：表示网址在互联网上能打开，和 Render 一样。数据仍然靠我们的 Google 登录保护：只有 `@advtransolution.com` 的账号能进，而且必须由管理层分配角色后才能看到数据。
- **`--max-instances 2`**：同一时间最多开 2 份网站。这样既控制了费用上限，也不会超出最小档数据库的连接数。3–5 个人用绰绰有余。

公司地址、电话这两项只打印在 BOL 上，地址里常带逗号，不方便写进上面的命令。部署完成后在网页上加：Cloud Run → tms → **Edit & deploy new revision** → Variables & Secrets，添加 `TMS_COMPANY_ADDRESS` 和 `TMS_COMPANY_PHONE`，然后点 Deploy。

---

## 第 8 步：把新网址加进 Google 登录

1. Google Cloud 控制台 → **Google Auth Platform**（旧名 APIs & Services → Credentials）→ Clients → 点开 TMS 用的那个 OAuth 客户端。
2. 在 **Authorized redirect URIs** 里**新增**一条（`PROJECT_NUMBER` 换成数字，可在 Cloud Shell 里运行 `echo $PROJECT_NUMBER` 查看）：
   ```
   https://tms-PROJECT_NUMBER.us-central1.run.app/accounts/google/login/callback/
   ```
   Render 那条先**不要删**，过渡期两边都能登录。
3. 保存。生效可能要几分钟。

---

## 第 9 步：检查和导入数据

1. 打开 `https://tms-PROJECT_NUMBER.us-central1.run.app/health/`，应该显示 ok。（注意不能用 `/healthz`：Cloud Run 会拦截以 z 结尾的路径。）
2. 打开首页，用 Google 登录。你应该直接是管理层。
3. 新数据库是空的：
   - 进 **团队 → 导入数据**，上传 `PTT-4475.json`（不用勾"清空旧数据"）。
   - 让 Jackie 和其他同事各登录一次，你在团队页给他们分配角色。
   - 再把 Jackie 设为 #4475 的负责人。

---

## 第 10 步：停掉 Render（试运行一周后）

确认 Google Cloud 上一切正常、大家都改用新网址以后：

1. Render → 删除 `tms` 网站和 `tms-db` 数据库。
2. Google 登录客户端里删掉 Render 那条回调地址。

---

## 以后更新代码

代码合并到 GitHub 的 main 分支后，在 Cloud Shell 里运行：

```bash
cd ~/tms && git pull && gcloud run deploy tms --source . --region us-central1
```

环境变量、Secret 和数据库连接都会保留，不用重新填。数据库结构的变更（migrations）会在网站启动时自动执行。

---

## 常见问题

**部署时报错，提到 `allUsers` 或 `iam.allowedPolicyMemberDomains`**

这是公司 Google Workspace 组织默认的安全策略：只允许本公司账号获得权限，所以"任何人可访问"这一项被拦下了。按 [Cloud Run 官方说明](https://cloud.google.com/run/docs/securing/managing-access#invoker_check)关闭调用方权限检查即可：

```bash
gcloud run services update tms --region us-central1 --no-invoker-iam-check
```

关掉后网址能打开，数据仍然只有登录后才能看到，原因和 `--allow-unauthenticated` 一样。

**构建失败，提示权限不足**

第 5 步的授权可能还没生效，等 2 分钟再运行第 7 步。如果仍然失败，看报错里提到的是哪个服务账号，把 `roles/run.builder` 授权给它。

**网站打开报 500，或者日志里有数据库连接错误**

查看最近的日志：

```bash
gcloud run services logs read tms --region us-central1 --limit 50
```

最常见的原因是 `CLOUDSQL_INSTANCE` 和 `--add-cloudsql-instances` 的值不一致，两处都应该是 `项目ID:us-central1:tms-db`。

**提示 DisallowedHost 或 CSRF 错误**

`DJANGO_ALLOWED_HOSTS` 必须和浏览器地址栏里的域名完全一致，前面不带 `https://`。

**想看花了多少钱**

Billing → Reports，按服务（Service）分组查看。
