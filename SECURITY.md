# Security Policy

本项目处理 App Store 销售数据和可向企业微信群发送消息的凭证。安全问题应优先于功能问题处理。

## 支持范围

仓库尚未发布版本。安全修复只针对默认分支的最新代码。

## 报告安全问题

如果仓库托管在 GitHub，请优先使用仓库的 `Security → Report a vulnerability` 私密报告功能。若该功能未启用，请联系仓库所有者并使用双方认可的私密渠道。

不要在公开 Issue、Pull Request、日志或截图中提供：

- Reporter Access Token。
- App Store Connect API 私钥、Issuer ID 与 Key ID 的完整组合。
- 企业微信 Webhook URL。
- Apple 原始销售报表。
- 可识别 App、开发者或销售地区的非公开数据。

报告中可以包含脱敏错误码、复现步骤、受影响版本和风险说明。

## 凭证处理

| 凭证 | 建议存储 | 泄露后的处理 |
| --- | --- | --- |
| Reporter Access Token | GitHub Secret 或 Secret Manager | 在 App Store Connect 重新生成并更新所有运行环境 |
| 企业微信 Webhook | GitHub Secret 或 Secret Manager | 删除或重建群机器人，替换 Webhook |
| App Store Connect `.p8` | Secret Manager，Base64 注入 | 撤销 API Key，创建新 Key |

Reporter Access Token 有效期为 180 天。同一 Apple Account 重新生成 Token 会使旧 Token 立即失效。

## 项目中的保护措施

- Reporter Token 不出现在命令行参数中。
- Token 只写入权限为 `0600` 的临时 properties 文件，退出后删除。
- Reporter.jar 从 Apple 官方地址下载，并校验固定 SHA-256。
- Webhook 只允许 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send`。
- 默认运行只生成本地报告；需要显式 `--send-wecom` 才发送消息。
- `.env`、`.p8`、`.cache/`、`output/` 和虚拟环境由 `.gitignore` 排除。
- GitHub Actions 的 `GITHUB_TOKEN` 只有 `contents: read` 权限。

## 原始数据

`output/raw/*.tsv.gz` 可能包含 App、商品、国家或地区、售价和开发者收益。它不应出现在公开仓库或公开 Artifact 中。

如需公开源码：

1. 不要在 Public 仓库运行当前收入工作流。公开仓库的读者或 Fork PR 可能访问 Actions Cache，不能用它保存原始日报。
2. 使用单独的 Private 部署仓库保存 Secrets、日报缓存和 Artifact。
3. 限制 Artifact 的访问与保留时间。
4. 定期清理本地缓存和过期 Artifact。
5. 确认企业微信机器人配置了合适的群范围、关键词或 IP 白名单。

## 响应流程

发现泄露后：

1. 立即撤销或轮换受影响凭证。
2. 暂停定时任务和企业微信发送。
3. 检查 Git 历史、Actions 日志、Artifact 和聊天记录。
4. 从历史中清理敏感内容；仅删除最新文件通常不足以消除 Git 历史泄露。
5. 使用新凭证执行一次受控验证。
6. 记录根因和防复发措施。
