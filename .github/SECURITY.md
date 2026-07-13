# Security Policy

## Supported Versions

Only the latest tagged CodexBar release receives security fixes. The project is currently in
the `0.x` series, so internal interfaces and the undocumented quota endpoint may change.

## Reporting a Vulnerability

Please use GitHub's private vulnerability reporting form:

<https://github.com/zhuxianghcl-ctrl/CodexBar/security/advisories/new>

Do not open a public Issue for a suspected credential leak, authentication bypass, unsafe
WebView behavior, or release-pipeline compromise. Do not attach real access tokens, refresh
tokens, API keys, `auth.json`, `credentials.dat`, or rollout files. Use synthetic examples and
the sanitized diagnostic export whenever possible.

The maintainer will acknowledge a complete report when available, assess affected versions,
and coordinate a fix and disclosure. No fixed response-time guarantee is offered for this
volunteer project.

## Security Boundaries

- `auth.json` is read-only.
- Cached OAuth credentials are protected with Windows DPAPI for the current user.
- TLS certificate and hostname verification are enabled by default.
- Codex SQLite databases and rollout files are read-only.
- CodexBar has no telemetry or remote log collection.

An attacker already able to run code as the same Windows user may be able to call DPAPI and
access that user's files. DPAPI does not protect against compromise of the signed-in account.
