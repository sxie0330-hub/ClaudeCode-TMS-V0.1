# ClaudeCode TMS V0.1

内部用的轻量运输管理系统，取代"一个客户/项目一份 Google Sheet"。目标是 3–5 人团队、普通办公电脑：页面全部由服务器生成，浏览器端没有任何框架。

## 解决的两个痛点

| 痛点 | 做法 |
|---|---|
| 表格太多，回溯时找不到 | 所有数据都在一个数据库里。顶部搜索框可以按柜号、MBL、Load号、序列号、PO、Monday ID、承运商 Load#、发票号搜。每票Load和每个项目都有完整时间线 |
| 单元格会被误改 | 页面默认只读，必须点"编辑"并保存。每次改动都记录谁、什么时候、旧值→新值，管理层可以一键回滚。Load送达后自动锁定，删除只是隐藏，可恢复。两个人同时编辑同一条记录时，后保存的人会被拦下 |

## 数据结构

```
客户 → 项目（可选）→ Load（一个柜子，或一车非集装箱货）
                       ├─ 提单 MBL（同一 MBL 的柜子共用一条记录）
                       ├─ 运输段（拖柜 / 堆场转运 / OTR / LTL …）
                       ├─ 货物明细（印在 BOL 上）
                       ├─ 设备序列号
                       ├─ 堆场停留（自动算天数，按月拆分）
                       ├─ 费用（应收 / 应付）
                       ├─ 文件链接（POD、照片、检验报告存 Google Drive）
                       └─ 时间线（改动记录 + 备注 + BOL 生成记录）
主数据：客户、承运商、地址库（码头 / 堆场 / 现场）
```

- **Load号** `SHP-YYMM-NNNN` 和**项目号** `PRJ-NNNN` 由系统生成，永不重复。Monday Item ID 作为可搜索的字段保存。
- **状态由日期自动推导**，不需要手工维护颜色：待安排 → 待到港 → 已到港 → 运输中 / 在堆场 → 已送达 → 已结案。只有 Hold 和取消需要手动设置。最后一个运输段送到最终收货地才算"已送达"。
- **LFD 临近**（默认 3 天内，且柜子还没离港）在列表中标红，并可以筛选出来。

## 我的工作台（V0.2）

- **首页由组件自由拼搭**：点「编辑布局」后可以拖动换位置、点尺寸改大小、移除组件，或从组件库添加。组件库共 17 种，包括今日待办、我的项目、置顶项目、关注的 Load、LFD 倒计时、POD 待收、待审额外费用、本月毛利、保存的筛选等。每个人的布局单独保存，互不影响。
- **「我的项目」组件**：显示哪些列、看哪个范围（只看我负责的 / 全部）、筛哪些状态，都可以自己设。
- **今日待办（按例外管理）**：17 条内置规则，按四类组织：港口与堆场、开工前检查、在途、回款与利润。每条规则可以单独开关，阈值、范围、级别也都能改；单条待办可以设"稍后提醒"；还可以自己加简单的条件规则。和钱有关的规则遵循角色权限，比如 AR 角色看不到应付相关的规则。
- **全局搜索**：输入时就弹出建议，按项目、Load、客户、承运商、地址、发票分组显示。按 `/` 随时唤出搜索框。支持筛选写法，例如 `客户:Hithium 承运商:Harris 状态:在途 POD:未收 公司:PTT`。
- **项目页**：顶部是汇总数字（售价、成本、毛利、附加费用、送达数、POD 数）。里程碑里的标准节点会根据 Load 自动生成，也可以加自定义节点。另有本项目待办、Load 明细、商务条款和时间线。
- **项目状态**：分五种——待执行、执行中、已交付待POD、已完结、已取消。前三种根据 Load 的进度自动变化；已完结和已取消由人手动设置。
- **Load 页**：
  - 用 7 个标准节点展示运输进度。
  - 填了到场、离场时间后，系统自动计算等待时长和可以收的 Detention。
  - 可以记录 POD 收到日期和审核结果。
  - 点「关注」，这个 Load 会出现在首页的"关注的 Load"组件里。

## AI 识别邮件（V0.3）

在项目页或 Load 页点「粘贴邮件 · AI 识别」：把承运商、司机或客户的邮件贴进去，Gemini 会识别出提货、送达、到场离场时间、司机、POD 等信息，列成“现在 → 改为”的对照。负责人勾选确认后才会写入。费用金额不会被识别或修改。每次写入都会留下修改记录，可以撤回。只有操作和管理层能用，调用费用由公司的 Google Cloud 项目统一支付。开通和使用方法见 [docs/ai-email.md](docs/ai-email.md)。

## 提醒

提醒只在登录后的「今日待办」里显示，包括柜子即将到港（按提单 ETA）和项目节点临近或过期。规则说明见 [docs/notifications.md](docs/notifications.md)。

## 角色与权限

| | 运输信息 | 应付 AP | 应收 AR | 毛利 | 解锁 / 回滚 / 删除Load / 团队 |
|---|---|---|---|---|---|
| 管理层 | 编辑 | ✓ | ✓ | ✓ | ✓ |
| 操作 OP | 编辑 | ✓ | — | — | — |
| 应付 AP | 只读 | ✓ | — | — | — |
| 应收 AR | 只读 | — | ✓ | — | — |

看不到的一侧费用，在费用表、时间线、CSV 导出和搜索里都不会出现。

## BOL

一套通用的 Straight BOL 版式，适用于整柜 HQ、框架箱、BESS 上 RGN，也适用于 53' 干货车装多托盘。

- 一张 BOL 对应一个运输段，BOL 号默认是 `Load号-段号`（如 `SHP-2610-0015-2`），可以手动改。
- 提货地、收货地、承运商取自运输段；货物取自货物明细；司机须知、UN 编号、紧急联系人取自项目设置。
- 列表或项目页里勾选多票，可以一次生成一个多页 PDF。

## 部署（Google Cloud）

正式环境用 Google Cloud Run + Cloud SQL，约 $10/月。逐步操作见 [docs/deploy-google-cloud.md](docs/deploy-google-cloud.md)。仓库里的 `Dockerfile` 就是给它用的。

## 部署（Render，免费测试用）

`render.yaml` 现在是**免费测试配置**：一个免费 Web 服务加一个免费 Postgres。

- 免费 Web 服务闲置 15 分钟后会休眠，下次打开要等大约 1 分钟。
- 免费 Postgres 在**创建 30 天后到期**，之后有 14 天宽限期，过了宽限期就会删除。所以只用来做内部测试，不要录真实业务数据。
- 正式上线时：把 `render.yaml` 里 Web 服务的 plan 改成 `starter`、数据库的 plan 改成 `basic-256mb`，并且用一个新的付费数据库存放真实数据。

部署步骤：

1. 在 Render 上选 **New → Blueprint**，选择这个仓库。
2. 按提示填写环境变量：
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`：见下一节
   - `TMS_BOOTSTRAP_ADMINS`：第一位管理层的邮箱，例如 `shaun@advtransolution.com`
   - `TMS_COMPANY_ADDRESS`、`TMS_COMPANY_PHONE`：印在 BOL 上（可选）
3. 部署完成后，用 Google 登录。`TMS_BOOTSTRAP_ADMINS` 里的账号自动成为管理层；其他同事登录后，由管理层在「团队」页面分配角色。
4. 测试数据：在「团队 → 导入 Trina 表格」上传 xlsx 即可，不需要服务器命令行。

### Google 登录设置（Google Cloud Console）

1. 新建或选择一个项目，打开 **APIs & Services → OAuth consent screen**，User type 选 **Internal**，这样只有公司 Workspace 账号能用。
2. 打开 **Credentials → Create credentials → OAuth client ID**，类型选 **Web application**。
3. **Authorized redirect URIs** 填 `https://<你的域名>/accounts/google/login/callback/`。
4. 把 Client ID 和 Client Secret 填进 Render 的环境变量。

系统另外会校验邮箱域名，只允许 `TMS_ALLOWED_EMAIL_DOMAINS` 里列出的域名登录。

## 导入数据（JSON）

团队页右上角点「导入数据」，上传 .json 文件即可，不需要命令行。格式说明写在 `core/importer.py` 文件开头。

- 同一客户下项目名称相同的，会自动跳过，所以重复导入不会产生重复数据。
- 可以先勾"只预览"，看一遍会导入哪些内容再正式写入。
- 测试期间如果要清空旧数据：勾选清空，并在确认框里输入 `DELETE`。
- 也可以用命令行导入：`python manage.py import_json file.json [--dry-run] [--wipe]`。

## 本地开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export DJANGO_DEBUG=1
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser      # 本地用 /dev-login/ 登录（仅 DEBUG 模式开放）
.venv/bin/python manage.py runserver
.venv/bin/python manage.py test core
```

## 技术栈

Django 5.2 · PostgreSQL · django-simple-history（审计）· django-allauth（Google 登录）· ReportLab（BOL PDF）· WhiteNoise · Gunicorn
