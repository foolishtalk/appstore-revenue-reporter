# Contributing

感谢你改进 App Store Revenue Reporter。提交代码前，请先确认变更不会泄露销售数据、Apple 凭证或企业微信 Webhook。

## 开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试不应依赖真实 Apple、Frankfurter 或企业微信服务。请使用现有 Fake Session、Mock 和临时目录。

## 提交变更

1. 先创建 Issue 描述问题或预期行为；小型修复可以直接提交 Pull Request。
2. 每个 Pull Request 聚焦一个问题。
3. 为行为变化增加或更新测试。
4. 修改统计口径时，同步更新 `docs/reporting-methodology.md`。
5. 修改部署、缓存或凭证时，同步更新 `docs/getting-started.md`、`docs/operations.md` 和 `.env.example`。
6. 运行完整测试，并在 Pull Request 中写明验证结果。

## 代码约定

- 支持 Python 3.11 及以上版本。
- 金额和汇率使用 `Decimal`，不要使用二进制浮点数进行最终计算。
- 按 TSV 列名解析，不依赖固定列位置。
- 外部请求必须设置超时，并对有限的可重试错误执行退避。
- 缺少币种、列或凭证时应明确失败，不要生成部分正确的报告。
- 默认行为不得发送企业微信；只有显式 `--send-wecom` 才允许外部写入。
- 不要降低 Reporter.jar 校验、Webhook 域名校验或临时凭证文件权限。

## 测试数据

禁止提交真实的：

- Reporter Access Token、JWT、`.p8` 私钥。
- 企业微信 Webhook 或机器人 key。
- Vendor Number、Account Number 与真实账号的组合。
- App 名称、SKU、Apple Identifier、销售国家或金额明细。
- `output/raw/` 中的 Apple 原始报表。

测试夹具应使用明显的虚构值，例如 `80012345`、`test-key` 和 `App A`。

## Pull Request 检查清单

- [ ] 新行为有测试覆盖。
- [ ] `python -m unittest discover -s tests -v` 通过。
- [ ] 文档与 `.env.example` 已同步。
- [ ] 没有包含凭证或真实业务数据。
- [ ] 没有改变 `Proceeds + Pacific Time` 统计契约，或已经清楚说明并获得确认。
- [ ] 没有执行与变更无关的格式化或重写。

## 安全问题

不要通过公开 Issue 报告凭证泄露或可利用的安全问题。请遵循 [SECURITY.md](SECURITY.md)。
