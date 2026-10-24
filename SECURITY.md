# Security Policy

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues or discussions.

Instead, please report security vulnerabilities privately using GitHub Private Vulnerability Reporting:
1. Navigate to the repository's **Security** tab on GitHub.
2. Under "Reporting", click **Report a vulnerability**.
3. Fill in the advisory form with details and steps to reproduce.

If private vulnerability reporting is unavailable, reach out to the repository maintainers privately.

### What to include

- A clear description of the vulnerability and potential security impact.
- Step-by-step instructions or a minimal proof of concept to reproduce the issue.
- Relevant system configuration, profile settings, or connector details.

## Scope

In scope:
- Flaws in pl CLI and core orchestration (`src/pl/`).
- Bypasses of connector permission gates or approval checks.
- Credential leaks or unintended disk storage of authentication tokens.
- Prompt injection vectors that trigger unauthorized tool execution or outward network calls.
- Sandbox escapes or file boundary violations.

Out of scope:
- Vulnerabilities in upstream harnesses (Claude Code, Codex, Antigravity) or third-party MCP servers.
- Attacks requiring root, physical, or arbitrary local code execution on the user machine.
- Denial-of-service attacks against local processes that cannot be triggered remotely.

## Response Times

- **Initial acknowledgment**: Within 48 hours.
- **Assessment and confirmation**: Within 5 business days.
- **Fix and advisory disclosure**: Coordinated release within 30 days for confirmed vulnerabilities.
