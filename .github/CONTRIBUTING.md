# Contributing to CodexBar

感谢你改进 CodexBar。项目只支持 Windows，使用 Python 3.13、uv、Tk、pywebview 和
PyInstaller。

## 开发环境

```powershell
uv sync --frozen --group build
uv run --frozen python -m unittest -q .\test_codexbar.py
uv run --frozen python -m compileall -q .\codexbar .\codexbar.pyw .\codexbar_dashboard.pyw .\test_codexbar.py
```

行为修改应先添加能复现需求或问题的失败测试，再完成最小实现。避免在同一个 PR 中混入
无关重构。

## 构建发布包 / Build a release

Releases 中的 EXE 是下面这套流程的产物。普通用户不需要执行；只有修改源码并准备发布新版本
的维护者才需要重新打包。

```powershell
uv sync --frozen --group build
.\scripts\build_exe.ps1
```

脚本使用 `uv.lock` 中固定的 PyInstaller，收集 Dashboard 资源、法律文件和第三方许可证，
并扫描发布目录中的凭据模式与本机路径。成功后生成：

```text
dist\CodexBar\CodexBar.exe
dist\CodexBar-Windows-x64.zip
dist\CodexBar-Windows-x64.zip.sha256
```

公开发布上传 ZIP 和对应的 SHA-256 文件。ZIP 是 one-folder 便携包，包含 EXE、HTML/CSS、
图标、Python 运行时和许可证；不能只分发单个 EXE。功能升级后重新运行同一脚本即可生成
新版本，用户保存在 `%LOCALAPPDATA%\CodexBar` 和 `~\.codex` 中的数据不会被覆盖。

源码模式的 Token 页面由 `codexbar_dashboard.pyw` 打开；打包后由同一个
`CodexBar.exe --dashboard` 打开。当前构建未签名；未来如加入代码签名，应先签名再计算
SHA-256。GitHub Actions 会运行同一套测试、构建和发布审计。

## 敏感数据

禁止提交或粘贴真实的 OAuth token、API key、account ID、`auth.json`、
`credentials.dat`、诊断原始日志、rollout、私人对话标题或本机绝对路径。测试必须使用
明显虚构的值和临时目录。

## Developer Certificate of Origin

项目采用轻量 DCO。每个提交请使用：

```powershell
git commit -s -m "type: concise description"
```

`Signed-off-by` 表示你有权按照本项目 MIT License 提交该贡献。DCO 文本见
<https://developercertificate.org/>。

## Pull Request

- 说明用户可见行为和安全影响。
- 列出实际运行的测试。
- 更新相关中英文文档。
- 保持 `auth.json` 只读，并确保日志不输出凭据。
- 不降低 TLS 校验或 GitHub Actions 权限。

提交贡献即表示你同意按仓库中的 MIT License 授权该贡献，并遵守
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
