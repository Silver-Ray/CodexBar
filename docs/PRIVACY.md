# CodexBar 隐私说明

CodexBar 是完全在用户 Windows 电脑上运行的开源桌面工具。项目不运营服务器，
不包含遥测、广告、分析 SDK 或远程崩溃上报。

## 本机读取

CodexBar 可能只读访问以下数据：

- `~/.codex/auth.json`：检测完整的 ChatGPT OAuth 登录。该文件不会被修改。
- Codex SQLite 和 rollout JSONL：统计本机 Codex 对话产生的 token 用量。
- 用户主动设置的模型价格、自定义 CA 证书和显示配置。

Token 用量页面可能在本机显示对话标题、模型名称和工作目录末级名称。这些信息只在
本地窗口中处理，不会由 CodexBar 上传。

## 本机写入

CodexBar 自有数据保存在 `%LOCALAPPDATA%\CodexBar`：

- `credentials.dat`：使用 Windows DPAPI 当前用户作用域加密的 OAuth 账号缓存。
- `config.json`：组件尺寸、颜色、字体和刷新间隔。
- `model_prices.json`：用户自定义模型价格。
- `error.log` 与 `error.log.1`：经过脱敏并限制大小的诊断日志。

旧版本可能在 `~/.codex/.codexbar_cfg.json` 保存显示配置。新版本只在首次迁移时读取
旧配置，不再写回该文件。

## 网络请求

CodexBar 仅为额度功能访问：

- `https://chatgpt.com/backend-api/wham/usage`
- `https://auth.openai.com/oauth/token`

请求会发送所选账号的 OAuth access token、账号 ID，或在刷新登录时发送 refresh token。
这是查询 ChatGPT/Codex 订阅额度所必需的数据。CodexBar 不会发送 rollout、对话标题、
工作目录、token 日报、模型价格或诊断日志。

如果设置系统代理或自定义 CA，网络流量会经过用户选择的代理。用户应只信任自己控制或
明确信任的代理与证书机构。

## 清除和卸载

右键 CodexBar 可以清除当前账号或全部账号。完整卸载需要：

1. 退出 CodexBar 并删除程序目录。
2. 删除 `%LOCALAPPDATA%\CodexBar`，清除加密账号、设置、价格和日志。
3. 如不再回滚旧版本，可自行删除 `~/.codex/.codexbar_cfg.json` 和
   `~/.codex/.quota_widget_cfg.json`。

仅删除 EXE 不会删除 `%LOCALAPPDATA%\CodexBar` 中的数据。CodexBar 永远不会自动删除
`~/.codex/auth.json` 或 Codex 对话记录。

## 诊断信息

提交问题时请使用“导出脱敏诊断信息”，不要上传 `auth.json`、`credentials.dat`、
rollout JSONL 或包含 token 的截图。自动脱敏是纵深保护，不应替代提交前人工检查。
