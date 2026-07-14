# 快速开始

本文介绍本地运行、凭证获取和 GitHub Actions 部署。统计定义请参阅[统计口径](reporting-methodology.md)。

## 环境要求

- Python 3.11 或更高版本
- Java 8 或更高版本
- 可以访问 Apple Reporter、App Store Connect 和 Frankfurter API 的网络
- 可选：企业微信群机器人

GitHub Actions 使用 Python 3.12 和 Java 17。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

项目首次运行时会从 Apple 官方地址下载 `Reporter.jar`，校验固定 SHA-256 后保存到 `.cache/apple-reporter/Reporter.jar`。如果 Apple 更新文件导致哈希变化，程序会停止，要求先审核新版文件。

## 获取 Apple 参数

### Reporter Access Token

1. 登录 [App Store Connect](https://appstoreconnect.apple.com/)。
2. 进入 `Trends`。
3. 打开 `Sales and Trends Reports`。
4. 在 `About Reports` 附近选择 `Generate Reporter Token`。
5. 复制生成的 Token。

生成 Token 的用户需要具备 `Admin`、`Finance` 或 `Sales with Reports` 权限。Token 有效期为 180 天；重新生成会立即使旧 Token 失效。

Reporter 已不再接受 Apple ID 密码或 App 专用密码。不要把个人 Apple ID 凭证交给脚本或第三方服务。

### Vendor Number

Vendor Number 通常是纯数字，可在 App Store Connect 的 `Payments and Financial Reports` 中查看，也可以通过 Reporter 的 `Sales.getVendors` 命令查询。

### Account Number

大多数账号不需要 `ASC_REPORTER_ACCOUNT`。只有 Reporter 返回错误码 `214`，表示当前 Apple Account 能访问多个 App Store Connect 账号时，才需要设置数字 Account Number。它不是 Apple ID 邮箱，可通过 `Sales.getAccounts` 查询。

## 获取企业微信 Webhook

在目标企业微信内部群中进入群设置，创建群机器人或自定义消息推送，复制完整 HTTPS Webhook：

```text
https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Webhook URL 相当于发送凭证。不要提交到 Git、Issue、日志或聊天截图中。

## 配置环境变量

最小配置：

```bash
export ASC_AUTH_METHOD='reporter'
export ASC_REPORTER_ACCESS_TOKEN='...'
export ASC_VENDOR_NUMBER='...'
```

发送企业微信时增加：

```bash
export WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
```

全部参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `ASC_AUTH_METHOD` | `reporter` | `reporter` 或 `api_key` |
| `ASC_REPORTER_ACCESS_TOKEN` | 无 | Reporter 模式必填 |
| `ASC_VENDOR_NUMBER` | 无 | 两种认证模式均必填 |
| `ASC_REPORTER_ACCOUNT` | 空 | 多账号时使用的数字 Account Number |
| `ASC_REPORTER_JAR` | 自动下载 | 手动指定官方 `Reporter.jar` |
| `ASC_REPORTER_JAVA` | `java` | Java 可执行文件路径 |
| `WECOM_WEBHOOK_URL` | 空 | 使用 `--send-wecom` 时必填 |
| `REPORT_END_DATE` | 最近通常可用日报日 | `YYYY-MM-DD`，按 PT 理解 |
| `REPORT_OUTPUT_DIR` | `output` | 输出与缓存目录 |

项目不会自动加载 `.env`。可参考 [.env.example](../.env.example)，再使用操作系统、CI Secret 或你自己的 Secret Manager 注入环境变量。

## 运行

只生成文件，不发送消息：

```bash
python scripts/fetch_appstore_revenue.py
```

生成并发送企业微信：

```bash
python scripts/fetch_appstore_revenue.py --send-wecom
```

补跑指定的 PT 截止日：

```bash
python scripts/fetch_appstore_revenue.py \
  --end-date 2026-01-31 \
  --send-wecom
```

首次运行需要取得 180 个日报日，用于滚动 90 天与此前 90 天的精确比较。后续会从 `output/raw/` 读取缓存，通常只请求新增的一天。

## 部署到 GitHub Actions

### 是否需要付费

不需要。推荐使用 GitHub Free 的 Private 仓库：

- 标准 Linux Runner 每月包含 2,000 分钟。
- Artifact 存储包含 500 MB。
- Actions Cache 每个仓库包含 10 GB。
- 当前本地 180 天原始日报、汇率和结果合计不到 1 MB。
- 首次回填完成后，每天通常只补一个日报；正常月度运行远低于 2,000 分钟。

如果账号没有有效付款方式，超出免费额度后 GitHub 会停止继续使用，而不是自动产生账单。可在 `Settings → Billing and licensing → Budgets and alerts` 设置预算与提醒。

Public 仓库的标准 GitHub-hosted Runner 可以免费运行，但不建议直接在那里部署真实任务。GitHub 官方明确提醒，不要把敏感信息放入 Actions Cache；公开仓库的读者或 Fork PR 可能读取默认分支缓存。当前工作流缓存 Apple 原始日报，并上传包含收益数据的 Artifact，因此应使用 Private 仓库。

参考：[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[Dependency caching security](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)。

如果希望公开源码，建议使用两个仓库：

1. Public 仓库：只保存 MIT 许可的源码和文档，不配置真实 Secrets，不运行收入任务。
2. Private 部署仓库：同步源码，配置 Reporter Token、Vendor Number 和 Webhook，启用定时任务。

### 配置步骤

在 GitHub 仓库中进入 `Settings → Secrets and variables → Actions`，创建：

- `ASC_REPORTER_ACCESS_TOKEN`
- `ASC_VENDOR_NUMBER`
- `WECOM_WEBHOOK_URL`
- `ASC_REPORTER_ACCOUNT`：仅多账号时需要

然后在 Actions 页面手动运行一次 `App Store Revenue Report`。确认：

1. 测试通过。
2. Apple 历史日报回填完成。
3. 企业微信群收到消息。
4. Artifact 中包含 JSON、Markdown、汇率和原始日报。

工作流每天 `12:00 UTC` 运行，即北京时间 `20:00`，对应 Pacific Time 的 `04:00` 或 `05:00`。任务默认获取最近通常可用的日报日，因此即使当天日报尚未发布，也会回退到前一个可用日期。原始日报和冻结汇率使用滚动 Actions Cache；Artifact 保留 30 天。

## 可选：Team API Key 模式

兼容模式使用 App Store Connect Team API Key：

```bash
export ASC_AUTH_METHOD='api_key'
export ASC_ISSUER_ID='...'
export ASC_KEY_ID='...'
export ASC_PRIVATE_KEY_BASE64='...'
export ASC_VENDOR_NUMBER='...'
```

Reporter Token 与 Team API Key 二选一即可。当前默认推荐 Reporter Token，因为权限范围更贴近本项目的报表下载用途。
