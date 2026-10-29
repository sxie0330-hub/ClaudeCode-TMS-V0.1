# AI 识别邮件：开通与使用

TMS 内置“粘贴邮件 → AI 识别 → 人工确认 → 写入”。识别用的是 Google 的 **Gemini**，调用由公司的 Google Cloud 项目（`atsstms`）统一付费：
- 同事不需要任何 AI 订阅；
- 只有登录 TMS 的人能用，且限于操作（OP）和管理层。

## 它会做什么、不会做什么

- **会**：
  - 从邮件里识别提货、送达的计划日期和实际日期，预约时刻和是否已确认，到场和离场时间，司机、电话、车牌，承运商单号，跟踪链接，船到港 ETA / 实际到港 ATA，LFD，POD 收到日期和链接，还空期限和还空日期；
  - 把其他事实（延误原因、故障、签收人等）记成时间线备注。
- **不会**：
  - 识别或修改任何金额、费用；
  - 未经确认写入任何东西。
- **写入时会再检查一遍**：
  - **权限**：和网页上一样。已送达锁定的 Load，只有管理层能改运输字段；POD、还空这类文件字段不受锁定限制。
  - **期间有没有人改过**：识别之后如果有人手动改过同一个字段，这一项不会覆盖，并标为“已被他人修改”。
- **留痕**：每次写入都会在修改记录里留下“AI 邮件识别 #编号”，像其他修改一样可以撤回。

## 开通（在 Cloud Shell 里做一次，约 5 分钟）

**一行完成：** 打开 Cloud Shell，粘贴下面这行。脚本会依次启用 Vertex AI、自动找到能用的 Gemini 模型、授权、部署新版本并打开开关：
```bash
cd ~/tms && git pull && bash scripts/enable_ai.sh
```

下面是脚本做的事情，分步写出来，方便排查：

打开 Google Cloud 控制台右上角的 Cloud Shell，按顺序粘贴下面各段。

**1. 启用 Vertex AI**
```bash
gcloud config set project atsstms
gcloud services enable aiplatform.googleapis.com
```

**2. 先确认模型能用。** 默认模型名是 `gemini-3.8-flash`：
```bash
curl -s -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/atsstms/locations/global/publishers/google/models/gemini-3.8-flash:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with the single word OK"}]}]}' | head -c 600; echo
```
- 输出里有 `"text": "OK"`，说明模型可用。
- 如果是 404 或 `NOT_FOUND`，说明模型名不对：到控制台的 **Vertex AI → Model Garden** 搜索 Gemini Flash，找到当前的模型名，替换上面命令里的名字，再测一次。后面第 4 步的 `TMS_AI_MODEL` 也用这个名字。

**3. 允许网站调用 Gemini**
```bash
gcloud projects add-iam-policy-binding atsstms \
  --member=serviceAccount:668605433770-compute@developer.gserviceaccount.com \
  --role=roles/aiplatform.user --condition=None --quiet >/dev/null && echo 已授权
```

**4. 更新网站并开启 AI 识别**
```bash
cd ~/tms && git pull
gcloud run deploy tms --source . --region us-central1 \
  --update-env-vars TMS_AI_ENABLED=1,TMS_AI_MODEL=gemini-3.8-flash
```
> 注意这里用的是 `--update-env-vars`（只增改这两项），**不要**写成 `--set-env-vars`，那样会清掉其他已有的设置。

部署完成后，导航栏会出现 **AI 识别**，项目页和 Load 页会出现 **粘贴邮件 · AI 识别** 按钮。

## 使用

### 新项目邮件（“项目开始”、新订单）
从 **项目** 列表点 **粘贴邮件 · AI 新建项目**，或者直接用导航栏的 **AI 识别**，把邮件贴进去。
- AI 会预填一张“新建项目”表单：项目名、客户、我方公司（ATS/PTT）、项目编号、送货地址、提货地（港口）、ETA 等里程碑、每个柜子 / 每车货一行 Load（柜号、柜型、MBL、车队）。
- 所有内容都可以改。系统里已有的柜号默认不勾选，避免重复。
- 点 **新建项目和勾选的 Load** 后，项目、Load、新地址、新客户和承运商会一次建好，负责人默认是你。
- 不会录入任何费用。

### 一次粘贴多个项目
一次可以粘贴好几个项目的信息，确认页会每个项目各给一张表单，可以分别新建，或者点"不建这个项目"跳过。

### 已有项目的后续批次
- AI 会对照"在执行的项目"清单。内容里的项目编号（如 #4560 / P-04560）、项目名或 PRJ 编号对得上时，会判定为这个项目的**新批次**。表单顶部"加到哪里"会预选这个项目，点 **添加勾选的 Load**，只会新增这批 Load 和勾选的里程碑，不建重复项目，也不改动项目本身的信息。
- 判断错了，在"加到哪里"里改成"新建一个项目"或别的项目即可。
- 从某个项目页点 **粘贴邮件 · AI 识别** 进入时，新柜子默认加到这个项目。
- 已经存在的柜号不会重复建。

### 后续补全信息
建项目时不全的信息，之后在邮件里出现时（手动粘贴或每天的自动扫描）AI 会提出补全，确认后写入：
- **Load**：柜号（"待定"的 Load 用系统 Load# 对应）、MBL、柜型、车队（没有运输段的会自动建一段）。
- **项目**：开始/结束日期、货物描述、送货地/提货地的名称、地址、城市、州、邮编、联系人。
- 柜号已经用在别的 Load 上时不会写入。
- 还没有柜号的项目，自动扫描会用"项目编号 + 客户名"（如 4915 + Sungrow，两者都要出现）找邮件。

### 地址、客户、承运商联想
- AI 确认页的地址代号、客户、车队输入框会联想：输入几个字母，就会列出地址库或客户、承运商名单里包含这几个字母的项。选中已有地址后，地址、城市、州、邮编会自动填好。
- 项目和 Load 编辑页里很长的下拉框上方都有搜索框：输入代号或名称里的几个字，就会直接选中第一个匹配项。

### 柜号很多 / 在表格里
两种方式都可以：
- 在 Google Sheets / Excel 里选中整块区域（包括表头），复制，粘贴到文本框。
- 直接上传表格文件：支持 .xlsx、.csv。Google Sheets 用 文件 → 下载 → Microsoft Excel (.xlsx)。

表格里系统已有的柜号会被识别为“更新”，没有的柜号会放进“新建项目”。一次超过约 300 行请分批。

### 更新已有 Load（跟踪邮件、POD 等）

1. 在项目页（或某个 Load 页）点 **粘贴邮件 · AI 识别**。
   - 从项目页进入：只在这个项目的 Load 里找对应；
   - 从 Load 页进入：只更新这一个 Load。
2. 把邮件整封复制进去，含往来回复都可以，点 **开始识别**，等 5–20 秒。
3. 在确认页逐行核对“现在 → 改为”和“邮件依据”。
   - 默认全部勾选，把不对的取消勾选。
   - 标注“邮件没写时区，按太平洋时间”的行要特别看一下：邮件没写时区时，系统按太平洋时间处理。
4. 点 **写入勾选的内容**。
   - 没勾选的会标为“未采用”；
   - 无法写入的会标明原因，比如 Load 已锁定，或者已被他人修改。

所有识别记录都在 **AI 识别** 页，可以查看邮件原文和每一项的处理结果。

## 费用

- 按 Gemini 的使用量计费，从 `atsstms` 的结算账号扣，试用期内用 $300 额度抵扣。
- 单价：据第三方汇总，Gemini 3.8 Flash 截至 2026-12-31 的优惠价是每百万 token 输入 $0.75、输出 $3.75，2027 年起翻倍。
- 一封邮件加上 Load 列表，大约是几千 token 的输入、几百 token 的输出。按这个价格，**每封估计在 1 美分以内**。这是估算，实际以 Billing → Reports 为准。
- 每人每 24 小时最多识别 200 次（`TMS_AI_DAILY_LIMIT`），防止误操作产生大量费用。

## 可调整的设置（Cloud Run 环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TMS_AI_ENABLED` | 关 | `1` 开启 |
| `TMS_AI_MODEL` | `gemini-3.8-flash` | Vertex AI 上的 Gemini 模型名 |
| `TMS_AI_LOCATION` | `global` | Vertex AI 区域 |
| `TMS_AI_PROJECT` | 网站所在项目 | 一般不用设 |
| `TMS_AI_DAILY_LIMIT` | `200` | 每人每 24 小时的识别次数上限 |

## 出错时

- **“无法获得 Google Cloud 凭据”**：第 3 步的授权没生效，等两分钟再试。
- **“Gemini 返回错误 403”**：第 1 步或第 3 步没完成。
- **“Gemini 返回错误 404”**：模型名不对，按第 2 步找到正确的名字，再用第 4 步的命令更新 `TMS_AI_MODEL`。
- 其他问题，查看网站日志：`gcloud run services logs read tms --region us-central1 --limit 50`。
