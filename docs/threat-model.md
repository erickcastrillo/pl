# Threat Model

pl coordinates AI coding agents and external connectors (such as chat, email, and issue trackers) to drive repository changes. This document outlines key threats, defensive layers, and the code boundaries that enforce them.

## Threats and Mitigation Layers

| Threat | Description | Mitigation Layer | Enforcing Code |
|---|---|---|---|
| **Tool Tampering** | An MCP connector server alters its advertised tools or version to introduce unauthorized capabilities. | Tool pinning and fingerprinting; disables write tools upon change until manually re-approved. | `src/pl/connectors/` (`approve-tools`) |
| **Prompt Injection** | Untrusted content from chat, email, or issue trackers attempts to manipulate agent behavior. | Treating inputs strictly as data; read-only gating; prompt injection test fixture suite. | `src/pl/guard.py`, `tests/` |
| **Agent Host Compromise** | An autonomous stage agent attempts to inspect host files, SSH keys, or other profile directories. | Stage sandboxing in a dedicated VM with only the workspace mounted; refusal of bypass-all-permissions modes. | `src/pl/sandbox.py`, `src/pl/assistant.py` |
| **Data Exfiltration** | An agent or compromised connector attempts to leak confidential source code or tokens externally. | Strict outbound network egress allow-lists in sandbox environments; zero credential pass-through. | `src/pl/sandbox.py`, `src/pl/harnesses.py` |
| **PII & Data Retention** | Personal contact information or email bodies persist indefinitely across cards and local logs. | Configurable data aging (e.g. 90-day retention pruning) and selective contact deletion (`pl forget`). | `src/pl/retention.py`, `src/pl/forget.py` |
| **Unaccountable Actions** | Actions or approvals executed without attribution, or abnormal automated send spikes. | Append-only, hash-chained audit logging and real-time anomaly detection alerts. | `src/pl/audit.py`, `src/pl/alerts.py` |
| **Supply Chain Risks** | Vulnerabilities in Python dependencies or CI GitHub Actions workflows. | Automated Dependabot scanning and locked dependency vulnerability audits via `pip-audit` in CI. | `.github/dependabot.yml`, `.github/workflows/ci.yml` |

## Trust Boundaries

1. **User / Owner**: Full control over profile configuration and explicit approval of plans and sensitive tool changes.
2. **AI Harnesses / Agents**: Operates on worktrees with least privilege; runs sandboxed during stage runs without bypass flags.
3. **External Connectors & Ingress**: All inbound events and trigger payloads are treated strictly as data, never direct shell commands.
4. **Local Host**: Host credentials, keys, and profiles are isolated from sandboxed stage executions.
