# 统计口径

本文定义企业微信日报中的指标、日期、汇率和对比方式。修改统计逻辑时，应同步修改本文和对应测试。

## 收益指标：Proceeds

日报使用 Apple Summary Sales Report 的以下字段：

```text
单行收益 = Units × Developer Proceeds
```

`Developer Proceeds` 是每单位开发者预估收益，已经扣除 Apple 佣金和适用税费。退款通常表现为负数 `Units`，会自然冲减收益。

本项目不使用 `Customer Price` 作为收入。`Customer Price` 对应用户支付额，即 App Store Connect 中的 Sales；例如一笔 USD 4.99 的销售，Developer Proceeds 可能是 USD 4.24。

## 销售数量指标：Units

日报同时汇总 Apple Summary Sales Report 中 `Developer Proceeds` 非零行的 `Units`，作为实际产生收益的销售数量。免费下载、重新下载和更新的 `Developer Proceeds` 为 0，不计入销售数量；退款通常是负数 `Units`，会冲减数量。因此显示的是扣除退款后的付费净销售数量。

销售数量与收入使用相同的当前周期和上一等长周期，并分别计算数量差和涨跌幅。销售数量不做汇率换算，也不与收入涨跌幅混用。

## 日期口径：Pacific Time

Reporter 下载的日报固定按 Pacific Time 划分，不能在请求中改成 UTC。程序使用 `America/Los_Angeles`：

- PT `08:00` 后，默认截止日为昨天。
- PT `08:00` 前，默认截止日为前天。

这样可以避开尚未生成的日报。App Store Connect 网页默认可能显示 UTC；对账时必须把后台时区切换为 `Pacific Time`，同时选择 `Proceeds` 指标。

只把报告日期标签改成 UTC 是错误的，因为金额仍属于 PT 日。Summary Sales Report 不包含逐笔交易时间，无法在本地重新切分成 UTC 日。

## 时间窗口

所有日期均包含开始日和结束日。

| 窗口 | 当前周期 | 对比周期 |
| --- | --- | --- |
| 最新可用日报日 | 截止日 | 前一日 |
| 最近 7 天 | 截止日及之前 6 天 | 紧邻的此前 7 天 |
| 最近 30 天 | 截止日及之前 29 天 | 紧邻的此前 30 天 |
| 滚动 90 天 | 截止日及之前 89 天 | 紧邻的此前 90 天 |

因此完整计算需要连续 180 个日报日。

## 涨跌幅

```text
涨跌额 = 本期 CNY - 上期 CNY
涨跌幅 = 涨跌额 ÷ |上期 CNY| × 100%
```

- 上升：企业微信使用绿色。
- 下降：企业微信使用红色。
- 金额相同：显示 `=`。
- 上期为 0、本期为正：显示“新增”。
- 上期为 0、本期为负：显示“转负”。

百分比保留最多一位小数；CNY 金额保留两位小数。百分比使用未提前舍入的金额计算。

销售数量的涨跌幅使用同一公式，其中 CNY 金额替换为净 `Units`；上期为 0 时同样显示“新增”“转负”或 `=`。

## CNY 管理汇率

不同 `Currency of Proceeds` 不能直接相加。项目采用按月冻结的管理汇率：

1. 对报表月份 M，取 M 上一个自然月的每日参考汇率。
2. 汇率接口返回 `1 CNY → X 收益币种`。
3. 每个日值先取倒数，得到 `1 收益币种 → X CNY`。
4. 对上一个自然月的日值做简单平均。
5. 生成 M 月固定汇率，并在整个月内复用。

数学表达：

```text
rate(currency → CNY, M)
  = average(1 / rate(CNY → currency, day))
    for day in previous_calendar_month(M)
```

汇率来自 [Frankfurter v2](https://frankfurter.dev/) 汇总的央行参考汇率，不需要 API Key。缓存文件位于：

```text
output/fx-rates/YYYY-MM.json
```

缓存记录来源日期、每个币种的观测数量和最终汇率。若缺少任何非零收益币种，程序会失败并列出币种，不会静默漏算。

管理汇率适合趋势比较，不代表 Apple 最终结算汇率。正式对账应使用 Payments and Financial Reports。

## Apple 查询粒度

Reporter 和 Sales Reports API 不支持任意 `start_date/end_date`。Summary Sales Report 只支持：

- Daily：单个 PT 日。
- Weekly：周一至周日，以周日作为报表日期。
- Monthly：单个自然月。
- Yearly：单个年份。

本项目使用 Daily，因为滚动 7、30、90 天及其上一周期的边界会每天移动，通常无法只用完整周报或月报精确表达。第一次运行会逐日回填 180 天；之后依靠缓存，日常运行通常只新增一次 Apple 请求。

## 为什么不直接使用 Apple 的 USD

Apple Analytics Reports API 的 `App Store Purchases` 报告提供 `Proceeds in USD`，但它与 Reporter 通道不同：

- 需要 App Store Connect Team API Key，而不是 Reporter Access Token。
- 首次创建报告请求需要 Admin 权限。
- 通常等待 1–2 天生成，日数据在事件发生约两天后完整。
- 报告请求按 App 创建，多 App 账号需要分别处理。

当前实现选择 Reporter，以保持次日可见并减少高权限凭证。如果以后接受约两天延迟，可以增加 Analytics Reports 数据源，再只做 USD→CNY 折算。

## 官方参考

- [Summary Sales Report](https://developer.apple.com/help/app-store-connect/reference/reporting/summary-sales-report)
- [Sales and Trends reports availability](https://developer.apple.com/help/app-store-connect/reference/reporting/sales-and-trends-reports-availability/)
- [View units, proceeds, sales, and pre-orders](https://developer.apple.com/help/app-store-connect/measure-app-performance/view-units-proceeds-sales-and-pre-orders)
- [Reporter User Guide](https://help.apple.com/itc/appsreporterguide/en.lproj/static.html)
- [Download sales and trends reports API](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-salesreports)
- [App Store Purchases Analytics Report](https://developer.apple.com/documentation/analytics-reports/app-store-purchase)
