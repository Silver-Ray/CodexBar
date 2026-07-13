## 变更说明

请说明用户可见行为、实现范围和不包含的内容。

## 验证

- [ ] 我先添加或确认了能覆盖行为的测试。
- [ ] `uv lock --check` 通过。
- [ ] 完整 unittest 和 compileall 通过。
- [ ] 涉及打包时，`scripts/build_exe.ps1` 和发行审计通过。
- [ ] 我更新了相关中文和英文文档。

## 安全与隐私

- [ ] 本 PR 不包含真实 token、API key、账号 ID、`auth.json`、vault、rollout、日志或私人路径。
- [ ] `auth.json` 仍保持只读，TLS 校验未降低。
- [ ] 新日志字段会经过脱敏，新增联网行为已明确披露。
- [ ] GitHub Actions 使用最小权限，第三方 Action 固定到完整提交 SHA。
- [ ] 我的提交包含 DCO `Signed-off-by`。
