# 自动部署：合并即上线

开通后，代码合并到 GitHub 的 `main` 分支时，GitHub 会自动：
1. 跑一遍全部测试，测试不过就**不部署**，线上保持原样；
2. 测试通过后部署到 Cloud Run，效果和手动运行 `gcloud run deploy tms --source . --region us-central1` 一样，环境变量、Secret 和数据库连接都保留；
3. 打开 `/health/` 检查网站正常。

全程约 5–8 分钟。进度和结果在 GitHub 仓库的 **Actions** 页查看。失败时 GitHub 会发邮件通知仓库所有者。

## 开通（在 Cloud Shell 里做一次）

```bash
cd ~/tms && git pull && bash scripts/enable_auto_deploy.sh
```

脚本做的事：
- 建一个只用于部署的服务账号 `github-deployer`，授予部署 Cloud Run 所需的权限；
- 用 Workload Identity Federation 让 GitHub 证明自己的身份，**不生成、不保存任何密钥**；
- 只接受 `sxie0330-hub/ClaudeCode-TMS-V0.1` 这个仓库的 `main` 分支，其他仓库、其他分支一律拒绝。

## 费用

- 构建本来就在 Cloud Build 上跑，和手动部署一样，每次几分钟；Cloud Build 每月有 2,500 分钟免费额度（e2-standard-2）。
- GitHub Actions：私有仓库在 GitHub Free 计划下每月有免费分钟数（约 2,000 分钟），每次部署大约用 6–10 分钟。

## 手动重新部署

GitHub → Actions → Test and deploy → **Run workflow**；或者照旧在 Cloud Shell 里运行部署命令。

## 关掉自动部署

GitHub → Actions → Test and deploy → 右上角 **…** → Disable workflow。
