# 发布 Windows 版本

发布仓库为 [Silver-Ray/CodexBar](https://github.com/Silver-Ray/CodexBar)。

1. 更新 `pyproject.toml`、`codexbar/__init__.py` 和 `codexbar/version_info.txt` 中的版本号，运行 `uv lock --default-index https://pypi.org/simple`。
2. 在 `docs/releases/<tag>.md` 写入该版本的发布说明，例如 `docs/releases/v0.2.0.md`。
3. 运行 `uv run --frozen python -m unittest discover -q`，提交并推送 main，查看 CI 是否通过。
4. 创建对应的版本标签并推送：

   ```powershell
   git tag -a v0.2.0 -m "CodexBar v0.2.0"
   git push origin refs/tags/v0.2.0
   ```

5. Release 工作流核对标签与项目版本，在 Windows 上运行全部测试、源码编译、构建、许可证收集和隐私审计，通过后发布 ZIP 与 SHA-256 文件。

如果标签推送没有启动发布，在 [Release 工作流](https://github.com/Silver-Ray/CodexBar/actions/workflows/release.yml) 选择 **Run workflow**，分支选择 **main**，`tag` 填入已有版本标签。工作流始终检出指定标签的源码，并核对实际版本。

CI 的构建产物保留 14 天；正式版本从 [Releases](https://github.com/Silver-Ray/CodexBar/releases) 下载。
