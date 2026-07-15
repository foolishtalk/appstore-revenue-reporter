# 运行维护

本文面向负责定时任务、凭证轮换和数据核对的维护者。

## 日常运行

GitHub Actions 每天 `17:17 UTC` 运行。该时间对应 Pacific Time 的 `09:17` 或 `10:17`，通常晚于 Apple 日报的 `08:00 PT` 发布时间。

工作流顺序：

1. 安装 Python 和 Java。
2. 运行单元测试。
3. 恢复日报与汇率缓存。
4. 下载缺失日报。
5. 获取缺失的月度汇率。
6. 生成 JSON 和 Markdown。
7. 发送企业微信。
8. 上传 Artifact。

## 首次运行

首次运行需要连续取得 180 个日报日。Reporter 不支持任意起止日期，因此程序逐日请求。完成后：

- 本地运行从 `output/raw/` 复用历史数据。
- GitHub Actions 从滚动 Cache 复用历史数据。
- 正常情况下每天只请求新增的一天。

不要中途删除 `output/raw/`，除非你有意重新下载全部历史日报。

## 手动补跑

```bash
python scripts/fetch_appstore_revenue.py \
  --end-date 2026-01-31 \
  --send-wecom
```

`--end-date` 按 Pacific Time 理解。补跑会覆盖 `revenue-summary.json` 和 `revenue-summary.md`，但不会删除其他日报或汇率缓存。

## 数据核对

当企业微信金额与 App Store Connect 网页不一致时，依次检查：

1. 网页时区是否为 `Pacific Time`，而不是默认 UTC。
2. 网页指标是否为 `Proceeds`，而不是 `Sales`。
3. 网页是否筛选了单个 App，而 Reporter 报告是否包含多个 App。
4. 企业微信的 Apple 报表截止日是否与网页选择日期一致。
5. 原始日报中的 `Units`、`Developer Proceeds` 和 `Currency of Proceeds`。
6. `output/fx-rates/` 中该报表月的冻结汇率。

Reporter 下载报表是最终化的日汇总；网页是近实时估算，两者在日报生成前可能暂时不同。

## 输出检查

```text
output/
├── fx-rates/
├── raw/
├── revenue-summary.json
└── revenue-summary.md
```

建议自动检查：

- `coverage.requested_days` 等于 180。
- `coverage.no_report_days` 符合预期。
- `exchange_rates.months` 覆盖所有有非零收益的报表月。
- 四个周期都包含 `comparison`。
- 四个周期都包含 `units`，且 `comparison` 中包含上期 `units`、`change_units`、`unit_change_percent` 和 `unit_direction`。
- Artifact 和企业微信发送步骤成功。

## 常见错误

| 错误 | 含义 | 处理 |
| --- | --- | --- |
| Reporter `117/211` | 报表延迟或服务暂不可用 | 等待自动重试，必要时稍后补跑 |
| Reporter `123` | Access Token 过期 | 重新生成并更新 Secret |
| Reporter `124/125` | Access Token 无效 | 检查 Token，确认没有多余空格 |
| Reporter `210` | 日报尚未生成 | PT `08:00` 后重试 |
| Reporter `213` | 当天没有销售单位 | 不是程序故障，按零收益处理 |
| Reporter `214` | Apple Account 关联多个账号 | 设置 `ASC_REPORTER_ACCOUNT` |
| Reporter `215/216` | Account Number 无效 | 使用 `Sales.getAccounts` 重新查询 |
| 汇率源缺少币种 | 无法生成完整 CNY 总额 | 检查币种代码和汇率源覆盖，不要手工忽略 |
| 企业微信 `errcode != 0` | Webhook 拒绝消息 | 检查机器人、关键词、IP 白名单和频率限制 |

## 凭证轮换

Reporter Access Token 有效期为 180 天。建议在到期前安排提醒：

1. 在 App Store Connect 生成新 Token。
2. 立即更新本地 Secret Manager 和 GitHub Secret。
3. 手动运行一次工作流。
4. 确认旧 Token 已失效且群消息正常。

Webhook 泄露时，应删除或重建群机器人并更新 `WECOM_WEBHOOK_URL`。Team API `.p8` 私钥泄露时，应立即撤销对应 API Key。

## 备份与保留

- `output/raw/` 可用于复算，但包含业务敏感数据。
- `output/fx-rates/` 应与相应汇总一起保留，保证汇率可追溯。
- GitHub Artifact 默认保留 30 天。
- 长期留存应使用访问受控的私有存储，并遵循组织的数据保留政策。
