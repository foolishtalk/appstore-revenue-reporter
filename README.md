# App Store Revenue Reporter

**English** | [简体中文](README.zh-CN.md)

Turn App Store Connect Sales and Trends reports into concise daily revenue updates for WeCom.

The reporter downloads Apple's official `Summary Sales Report`, calculates estimated developer revenue as `Units × Developer Proceeds`, converts proceeds from multiple currencies to CNY, and compares each reporting window with the immediately preceding period of equal length.

> Reporting contract: **Proceeds + Pacific Time + CNY**. Revenue means estimated developer proceeds—not customer-paid Sales or final financial settlement.

## Features

- Authenticate with a Reporter Access Token; no Apple ID password or app-specific password required.
- Report revenue and paid net units for the latest daily report, trailing 7 days, trailing 30 days, and rolling 90 days.
- Compare revenue and sales units with the preceding day or equal-length period.
- Convert multi-currency proceeds to CNY using reference rates frozen by report month.
- Generate WeCom-compatible Markdown and an auditable JSON summary.
- Show sales and refund quantities grouped by the actual product `Title`.
- Exclude free downloads, re-downloads, updates, zero-net-unit products, and `CMB-C` bundle credits where appropriate.
- Cache raw Apple reports and monthly exchange rates so routine runs only fetch new data.
- Include a GitHub Actions schedule, manual backfills, and workflow artifacts.

## Example Output

The generated WeCom report is currently written in Chinese:

```text
# App Store 收入日报

> Apple 报表统计截止日（太平洋时间）：2026-01-31

最近 7 天　¥800.00　+14.3%
> 销售数量 120　+20%；
> 上期 100；
> 销售项目：示例项目 A × 80；
> 销售项目：示例项目 B × 40；
> 退款数量 3；
> 退款项目：示例项目 A × 2；
> 退款项目：示例项目 B × 1；
```

The example only demonstrates the format. Actual messages also include all reporting windows, date ranges, and previous-period revenue. A data-coverage warning appears only when one or more daily reports are unavailable.

## Quick Start

Requirements: Python 3.11+ and Java 8+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ASC_REPORTER_ACCESS_TOKEN='...'
export ASC_VENDOR_NUMBER='...'

python scripts/fetch_appstore_revenue.py
```

To send the report to WeCom:

```bash
export WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
python scripts/fetch_appstore_revenue.py --send-wecom
```

See the [getting-started guide](docs/getting-started.md) for credential setup, configuration options, and GitHub Actions deployment.

> Deploy real scheduled jobs in a **private repository**. This keeps raw sales-report caches and workflow artifacts from being exposed publicly.

## Reporting Methodology

> Revenue: `Units × Developer Proceeds`, converted to CNY.
>
> Sales units: paid net `Units`, excluding free downloads, re-downloads, and updates.
>
> Comparisons: each window is compared with the immediately preceding period of equal length.
>
> Exchange rates: each report month uses the previous calendar month's average daily reference rates, frozen for the month.
>
> Data status: Sales and Trends provides estimated proceeds; final settlement is determined by Finance Reports.

| Item | Method |
| --- | --- |
| Revenue | `Developer Proceeds`, aggregated as `Units × Developer Proceeds` |
| Sales units | Paid net `Units` from rows with non-zero `Developer Proceeds`, grouped by `Title`; zero-net-unit products are hidden |
| Refund units | Absolute quantity of negative `Units`, grouped by `Title`; excludes `CMB-C` bundle credits |
| Report timezone | `America/Los_Angeles` (Pacific Time) |
| Display currency | CNY |
| Exchange rates | Each report month uses the previous calendar month's average daily reference rates, frozen for the month |
| Daily comparison | Current report day versus the preceding day |
| 7/30/90-day comparison | Current period versus the immediately preceding equal-length period |
| Data status | Estimated Sales and Trends proceeds, not final Finance Report settlement |

App Store Connect may default to UTC and Sales. For reconciliation, select `Pacific Time` and `Proceeds`. See the [reporting methodology](docs/reporting-methodology.md) for formulas, exchange-rate policy, and Apple report limitations.

## Configuration

Reporter authentication is the default and only requires:

| Environment variable / GitHub Secret | Required | Description |
| --- | --- | --- |
| `ASC_REPORTER_ACCESS_TOKEN` | Yes | Apple Reporter Access Token |
| `ASC_VENDOR_NUMBER` | Yes | App Store Connect Vendor Number |
| `WECOM_WEBHOOK_URL` | When sending | Full WeCom group-bot webhook URL |
| `ASC_REPORTER_ACCOUNT` | Multi-account only | Reporter Account Number; needed only for error code `214` |

Copy [.env.example](.env.example) to review all optional settings. App Store Connect Team API Key authentication remains available as a compatibility mode.

## Project Structure

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

Each run generates:

```text
output/
├── fx-rates/              # CNY rates frozen by report month
├── raw/                   # Cached raw Apple daily reports
├── revenue-summary.json   # Machine-readable result and audit details
└── revenue-summary.md     # WeCom Markdown
```

## Development

```bash
python -m unittest discover -s tests -v
```

Tests cover Reporter authentication, temporary credential files, retries, gzip/TSV parsing, CNY conversion, period comparisons, sales and refund details, WeCom delivery, and security validation. Read [CONTRIBUTING.md](CONTRIBUTING.md) before contributing.

## Documentation

- [Getting started and deployment](docs/getting-started.md)
- [Reporting methodology and reconciliation](docs/reporting-methodology.md)
- [Architecture and data flow](docs/architecture.md)
- [Operations and troubleshooting](docs/operations.md)
- [Security policy](SECURITY.md)

## Security

Reporter tokens, webhook URLs, `.p8` private keys, and raw sales reports are sensitive. Never commit `.env`, `output/`, `.cache/`, or real report samples. Rotate exposed credentials immediately by following [SECURITY.md](SECURITY.md).

## License

This project is licensed under the [MIT License](LICENSE).
