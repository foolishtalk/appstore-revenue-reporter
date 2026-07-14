# App Store Revenue Reporter

把 App Store Connect 的 Sales and Trends 报表整理成一条可读的企业微信收益日报。

项目使用 Apple 官方 Reporter 下载 `Summary Sales Report`，按 `Units × Developer Proceeds` 计算开发者预估收益，将不同结算币种统一折算为 CNY，并展示与上一等长周期相比的涨跌幅。

> 当前统计契约：**Proceeds + Pacific Time + CNY**。这里的收入指开发者预估收益，不是用户支付的 Sales，也不是最终财务结算。

## 功能

- 使用 Reporter Access Token，不需要 Apple ID 密码或 App 专用密码。
- 输出最新日报日、最近 7 天、最近 30 天和滚动 90 天收益。
- 自动计算前一日、前 7 天、前 30 天和前 90 天的环比变化。
- 使用上一个自然月的日均参考汇率，将多币种收益折算为 CNY。
- 生成适合企业微信机器人的 Markdown，以及包含审计明细的 JSON。
- 缓存 Apple 原始日报与月度冻结汇率，日常运行只补充新数据。
- 提供 GitHub Actions 定时任务、手动补跑和 Artifact 输出。

## 输出示例

```text
# App Store 收入日报

> Apple 报表统计截止日（太平洋时间）：2026-01-31
> 收入口径：Units × Developer Proceeds，统一折算为 CNY
> 对比口径：与紧邻的上一等长周期相比

最近 7 天　¥800.00　+14.3%
最近 30 天　¥3,200.00　-5.9%
最近一个季度（滚动 90 天）　¥9,600.00　+2.1%
```

示例只用于展示格式。实际消息还会列出各窗口日期、上一周期金额、数据覆盖和汇率口径。

## 快速开始

运行环境：Python 3.11+、Java 8+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ASC_REPORTER_ACCESS_TOKEN='...'
export ASC_VENDOR_NUMBER='...'

python scripts/fetch_appstore_revenue.py
```

发送到企业微信：

```bash
export WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
python scripts/fetch_appstore_revenue.py --send-wecom
```

完整的凭证获取、参数说明和 GitHub Actions 部署步骤见[快速开始](docs/getting-started.md)。

> 推荐把真实定时任务部署到 **Private 仓库**。GitHub Free 的私有仓库额度足够运行本项目，同时避免公开销售报表缓存和 Artifact。

## 统计口径

| 项目 | 口径 |
| --- | --- |
| 收益指标 | `Developer Proceeds`，按 `Units × Developer Proceeds` 汇总 |
| 报表时区 | `America/Los_Angeles`，即 Pacific Time |
| 展示币种 | CNY |
| 汇率 | 报表月使用上一个自然月的日均参考汇率，月内冻结 |
| 日报比较 | 当前日报日与前一日 |
| 7/30/90 天比较 | 与紧邻的上一等长周期 |
| 数据性质 | Sales and Trends 预估收益，不是 Finance Report 最终结算 |

App Store Connect 网页默认可能显示 UTC 和 Sales。核对数据时，请在后台选择 `Pacific Time` 和 `Proceeds`。详细计算公式、汇率策略和 Apple 报表限制见[统计口径](docs/reporting-methodology.md)。

## 配置

Reporter 模式是默认认证方式，只需要：

| 环境变量 / GitHub Secret | 必填 | 说明 |
| --- | --- | --- |
| `ASC_REPORTER_ACCESS_TOKEN` | 是 | Apple Reporter Access Token |
| `ASC_VENDOR_NUMBER` | 是 | App Store Connect Vendor Number |
| `WECOM_WEBHOOK_URL` | 发送时 | 企业微信群机器人完整 Webhook URL |
| `ASC_REPORTER_ACCOUNT` | 多账号时 | Reporter Account Number；仅错误码 `214` 时需要 |

可复制 [.env.example](.env.example) 查看全部可选项。项目也保留了 App Store Connect Team API Key 兼容模式。

## 项目结构

```text
.
├── .github/
│   └── workflows/appstore-revenue.yml
├── docs/
│   ├── architecture.md
│   ├── getting-started.md
│   ├── operations.md
│   └── reporting-methodology.md
├── scripts/
│   └── fetch_appstore_revenue.py
├── tests/
│   └── test_fetch_appstore_revenue.py
├── CONTRIBUTING.md
├── LICENSE
├── SECURITY.md
└── requirements.txt
```

运行后会生成：

```text
output/
├── fx-rates/              # 按报表月冻结的 CNY 汇率
├── raw/                   # Apple 原始日报缓存
├── revenue-summary.json   # 机器可读结果及审计信息
└── revenue-summary.md     # 企业微信 Markdown
```

## 开发

```bash
python -m unittest discover -s tests -v
```

当前测试覆盖 Reporter 认证、临时凭证文件、错误重试、gzip/TSV 解析、CNY 汇率、周期比较、企业微信发送和安全校验。参与开发前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档

- [快速开始与部署](docs/getting-started.md)
- [统计口径与对账方法](docs/reporting-methodology.md)
- [架构与数据流](docs/architecture.md)
- [运行维护与故障排查](docs/operations.md)
- [安全策略](SECURITY.md)

## 安全

Reporter Token、Webhook、`.p8` 私钥和原始销售报表都属于敏感信息。不要提交 `.env`、`output/`、`.cache/` 或真实报表样本。发现凭证泄露时应立即轮换，具体流程见 [SECURITY.md](SECURITY.md)。

## License

本项目使用 [MIT License](LICENSE)。
