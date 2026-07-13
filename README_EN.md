# CodexBar

English | [简体中文](README.md)

CodexBar is a privacy-conscious Windows taskbar widget for Codex quota and local token usage.
It can cache multiple ChatGPT OAuth accounts with Windows DPAPI, display available quota
windows beside the notification area, and build a local token ledger from Codex SQLite and
rollout JSONL files. The ledger includes daily trends, conversation rankings, token categories,
and an API-equivalent cost estimate.

<p align="center">
  <img src="docs/images/taskbar-widget.png" alt="CodexBar taskbar widget" width="645">
</p>

> [!IMPORTANT]
> CodexBar is a community project and is not affiliated with OpenAI, endorsed by OpenAI, or an
> official OpenAI product. It does not include telemetry, advertising, or remote log upload.
> Quota lookup uses an undocumented endpoint, and the local Codex log format is not a stable
> public API. Upstream changes can temporarily break quota or token reporting. Dollar values
> are estimates based on public API prices, not subscription bills or actual charges.

## Quick Start

CodexBar is a portable app. It does not require Python or a traditional installer:

1. Download the latest `CodexBar-Windows-x64.zip` from
   [GitHub Releases](https://github.com/zhuxianghcl-ctrl/CodexBar/releases).
2. Extract the complete ZIP.
3. Open the extracted `CodexBar` folder and run `CodexBar.exe`.

Keep the complete folder together because the dashboard assets and Python runtime sit beside the
EXE. Public builds are currently unsigned, so Windows SmartScreen may identify the publisher as
unknown. Continue only when the archive came from this repository's Releases page. SHA-256
verification is available later in this README as an optional advanced safety check.

On first use, an existing ChatGPT OAuth login is detected automatically. If the widget shows
`AUTH`, run `codex login --device-auth`, finish the browser login, and click the widget to refresh.
Click refreshes, double-click opens Settings, and right-click opens the full menu.

## Run From Source (Developers)

Regular users do not need this section. The ZIP on Releases already contains the packaged EXE and
its runtime.

Install [uv](https://docs.astral.sh/uv/) and Git on Windows 10 or Windows 11, then clone the
repository. The included `启动 CodexBar.bat` launcher synchronizes the locked environment and
starts the widget. You can also run:

```powershell
uv sync --frozen
uv run --frozen pythonw .\codexbar.pyw
```

A session-scoped Windows named mutex keeps the taskbar widget single-instance. Starting it a
second time exits the new process silently and leaves the existing instance untouched.

## What It Shows

- Available quota windows. A window missing from the server response is shown as `--`.
- Today's total tokens and API-equivalent estimated cost in the taskbar widget.
- Daily input, cached-input, output, and reasoning usage in the Token Usage dashboard.
- Seven-day, current-month, and current-year trends.
- Per-conversation rankings using locally available renamed titles when possible.
- Multiple cached OAuth accounts with a taskbar menu for switching the displayed account.

Token usage is aggregated across all locally recorded Codex tasks and is not separated by
ChatGPT account. Dates use the Windows local time zone. Child-task history copied into another
rollout is de-duplicated using cumulative token snapshots and handoff boundaries where the
local event format provides them.

## Screenshots

### Desktop Overview

![CodexBar on the Windows desktop and taskbar](docs/images/desktop-overview.jpg)

### Settings

<p align="center">
  <img src="docs/images/settings-window.png" alt="CodexBar settings window" width="377">
</p>

## Quota And Pricing Limitations

Quota requests use `https://chatgpt.com/backend-api/wham/usage`, an undocumented endpoint that
is not covered by a public stability guarantee. OAuth refreshes use
`https://auth.openai.com/oauth/token`. TLS certificate and hostname verification remain enabled.

Cost figures use input, cached-input, and output token counts from local events. Reasoning tokens
are displayed separately but are already part of output pricing and are not charged twice.
Built-in prices are a release-time snapshot of the OpenAI Standard API prices. Check the
[OpenAI model comparison page](https://developers.openai.com/api/docs/models/compare) and use
the price editor when a model is unknown or its price changes. Estimates do not represent a
ChatGPT Plus, Pro, Team, or Enterprise bill.

## Local Data And Privacy

CodexBar reads these existing files without modifying them:

- `~/.codex/auth.json`, to detect a complete ChatGPT OAuth login.
- Codex SQLite databases and rollout JSONL files, to calculate local token usage.

CodexBar writes only its own application data under `%LOCALAPPDATA%\CodexBar`:

- `credentials.dat`: OAuth account cache encrypted with current-user Windows DPAPI.
- `config.json`: widget and refresh settings.
- `model_prices.json`: optional user price overrides.
- `error.log` and `error.log.1`: size-limited, sanitized diagnostics.

Conversation titles, rollouts, token reports, price overrides, and diagnostic logs are not sent
by CodexBar. The quota request necessarily sends the selected OAuth credential and account ID to
the OpenAI authentication or ChatGPT host. See [docs/PRIVACY.md](docs/PRIVACY.md) for the complete boundary.

An attacker already running code as the same signed-in Windows user may be able to use DPAPI and
read that user's files. DPAPI protects data at rest from other users; it does not protect a
compromised Windows session.

<details>
<summary>Optional: verify the downloaded SHA-256</summary>

Download `CodexBar-Windows-x64.zip.sha256` for the same release, then run these commands beside
the ZIP:

```powershell
(Get-FileHash .\CodexBar-Windows-x64.zip -Algorithm SHA256).Hash.ToLowerInvariant()
Get-Content .\CodexBar-Windows-x64.zip.sha256
```

The two 64-character hexadecimal values should match exactly. This step is optional for normal
use.

</details>

## Diagnostics

`ERR` indicates an unexpected refresh error, often a temporary proxy, network, upstream HTTP,
or local-file condition. The taskbar menu can open the local error log or export a sanitized
diagnostic report. Sanitization is defense in depth, so inspect every report before sharing it.
Never attach real tokens, account IDs, `auth.json`, `credentials.dat`, raw rollouts, or unchecked
logs to a public Issue.

## Uninstall

1. Exit CodexBar and delete the extracted application directory or source checkout.
2. Delete `%LOCALAPPDATA%\CodexBar` to remove credentials, settings, prices, and diagnostics.
3. Delete any shortcut you created.
4. If rollback is no longer needed, optionally remove legacy `.codexbar_cfg.json`,
   `.quota_widget_cfg.json`, and `%LOCALAPPDATA%\CodexQuota` data.

CodexBar never deletes `~/.codex/auth.json`, Codex databases, or rollout conversation history.

## Development

```powershell
uv lock --check
uv sync --frozen --group build
uv run --frozen python -m unittest -q .\test_codexbar.py
uv run --frozen python -m compileall -q .\codexbar .\codexbar.pyw .\codexbar_dashboard.pyw .\test_codexbar.py
```

Tests use temporary auth, configuration, vault, database, and HTTP fixtures. They do not access
your real Codex login. Contributions should add a failing behavior test first and use DCO
sign-off with `git commit -s`.

## Project Policies

- License: [MIT LICENSE](LICENSE)
- Privacy: [docs/PRIVACY.md](docs/PRIVACY.md)
- Security: [.github/SECURITY.md](.github/SECURITY.md)
- Contributing: [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md)
- Third-party licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Code of conduct: [.github/CODE_OF_CONDUCT.md](.github/CODE_OF_CONDUCT.md)
