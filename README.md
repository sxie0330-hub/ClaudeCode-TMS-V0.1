# ClaudeCode TMS V0.1

内部用的轻量运输管理系统，取代"一个客户/项目一份 Google Sheet"。目标是 3–5 人团队、普通办公电脑：页面全部由服务器生成，浏览器端没有任何框架。

## 解决的两个痛点

| 痛点 | 做法 |
|---|---|
| 表格太多，回溯时找不到 | 所有数据都在一个数据库里。顶部搜索框可以按柜号、MBL、运单号、序列号、PO、Monday ID、承运商 Load#、发票号搜。每票运单和每个项目都有完整时间线 |
| 单元格会被误改 | 页面默认只读，必须点"编辑"并保存。每次改动都记录谁、什么时候、旧值→新值，管理层可以一键回滚。运单送达后自动锁定，删除只是隐藏，可恢复。两个人同时编辑同一条记录时，后保存的人会被拦下 |

## 数据结构

```
客户 → 项目（可选）→ 运单（一个柜子，或一车非集装箱货）
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

- **运单号** `SHP-YYMM-NNNN` 和**项目号** `PRJ-NNNN` 由系统生成，永不重复。Monday Item ID 作为可搜索的字段保存。
- **状态由日期自动推导**，不需要手工维护颜色：待安排 → 待到港 → 已到港 → 运输中 / 在堆场 → 已送达 → 已结案。只有 Hold 和取消需要手动设置。最后一个运输段送到最终收货地才算"已送达"。
- **LFD 临近**（默认 3 天内，且柜子还没离港）在列表中标红，并可以筛选出来。

## 角色与权限

| | 运输信息 | 应付 AP | 应收 AR | 毛利 | 解锁 / 回滚 / 删除运单 / 团队 |
|---|---|---|---|---|---|
| 管理层 | 编辑 | ✓ | ✓ | ✓ | ✓ |
| 操作 OP | 编辑 | ✓ | — | — | — |
| 应付 AP | 只读 | ✓ | — | — | — |
| 应收 AR | 只读 | — | ✓ | — | — |

看不到的一侧费用，在费用表、时间线、CSV 导出和搜索里都不会出现。

## BOL

一套通用的 Straight BOL 版式，适用于整柜 HQ、框架箱、BESS 上 RGN，也适用于 53' 干货车装多托盘。

- 一张 BOL 对应一个运输段，BOL 号默认是 `运单号-段号`（如 `SHP-2610-0015-2`），可以手动改。
- 提货地、收货地、承运商取自运输段；货物取自货物明细；司机须知、UN 编号、紧急联系人取自项目设置。
- 列表或项目页里勾选多票，可以一次生成一个多页 PDF。

## 部署（Render）

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

## 导入 Trina 测试数据

```bash
python manage.py import_trina "Trina NJ_VA units Tracking Sheet.xlsx"            # 正式导入
python manage.py import_trina "Trina NJ_VA units Tracking Sheet.xlsx" --dry-run  # 只看读到什么
```

也可以不用命令行，直接用网页：管理层登录后打开「团队 → 导入 Trina 表格」上传文件。重复导入是安全的，已经导入过的会自动跳过。客户表格（`*.xlsx`）不进仓库。

导入时的处理规则：
- Trina 自己的 BESS 箱（CYMU…）一箱一票。
- KOCU… 是海运柜，里面的 BIC 设备后来分批送往不同现场，所以按"柜号 + 项目 + 堆场"拆成多票。
- CA 起运地址和部分现场地址在原表里不完整，导入时原样保存，需要在地址库里补全。

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
