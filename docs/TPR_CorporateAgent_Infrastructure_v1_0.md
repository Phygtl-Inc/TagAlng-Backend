# TPR v1.0 — Corporate AI Agent Infrastructure

**Product:** Lana · Phygtl, Inc.
**Owner:** Data & AI
**Reference agent:** Argus (Data Governance)
**Date:** July 2026
**Status:** Active

---

> **Scope:** This document defines the infrastructure requirements to run Argus — and any future corporate AI agent — in a production-grade, always-on, bidirectional configuration. It is written generically so it serves as the baseline for all corporate agents, not just Argus.

---

## 1. Context — Why This Infrastructure Is Needed

Argus currently runs on a local WSL2 machine (Windows). This works for outbound messaging — Argus can send reports to Telegram and Slack. However it has three critical limitations in this configuration:

- **No persistence** — Argus only runs when the machine is on and WSL2 is manually started.
- **No inbound messaging** — without a public URL, Slack cannot send webhook events back to Argus. @Argus mentions are impossible.
- **Personal credentials** — Argus uses personal Supabase keys and a personal GCP project. This is a security and continuity risk.

Moving Argus to a persistent, cloud-hosted environment with corporate credentials resolves all three.

---

## 2. Requirements

| Priority | Requirement | Owner | Unblocks | Status |
|---|---|---|---|---|
| 🔴 P1 | **Persistent Linux environment with public URL** — A VM or Cloud Run instance running 24/7 on GCP. Required for the Hermes gateway to receive inbound Slack events. Without a public URL, the agent can send messages but cannot receive them — making @Argus mentions and two-way interaction impossible. | Backend | Slack bidirectional, persistent operation | Pending |
| 🔴 P1 | **Dedicated Supabase service role key (read-only)** — A service role key scoped to the production Supabase project, created specifically for Argus. Must not be a personal key or the anon key. Read access to all public tables. Write access not required at this stage. | Backend | Production database monitoring | Pending |
| 🔴 P1 | **Slack app approval + bot in channel** — The Argus Slack app must be approved by a workspace admin and invited to the target channel (#11-analytics). Token stored in agent .env as `SLACK_BOT_TOKEN`. | Both | Slack outbound (done) + inbound (pending URL) | Partial — outbound working |
| 🟡 P2 | **GCP access or independent VPS** — Data & AI needs either: (a) permission to create GCP resources in the Phygtl org, or (b) an independent VPS (e.g. DigitalOcean, Render) that Data & AI controls directly. Option (b) removes Backend dependency entirely for agent hosting. | Data & AI | Autonomous agent operations without Backend bottleneck | Pending — question for Backend |
| 🟡 P2 | **GitHub fine-grained PAT approval** — The `Hermes_Token_AI_Governance` PAT created by Data & AI is pending org admin approval. Required for Argus to open PRs with governance artifacts automatically. | Backend | Automated governance PR workflow | Pending org admin approval |
| 🟡 P2 | **Google Sheets service account under Phygtl GCP org** — The `lana-argus-sheets` GCP project and service account must be confirmed under the Phygtl org, not a personal Google account. Required for governance artifact sync to remain accessible if personnel changes. | Backend | Governance artifact availability | Pending confirmation |
| ⚪ P3 | **Slack Event Subscriptions configured** — Once the public URL is available, configure Slack Event Subscriptions in the Argus app settings to receive `app_mention` and `message.channels` events. Required for true bidirectional @Argus interaction. | Data & AI | Full bidirectional Slack integration | Blocked on P1 |
| ⚪ P3 | **Corporate LLM policy for internal agents** — Define whether internal agents must use Vertex AI / Gemini, or whether Anthropic API is acceptable. Argus currently uses Anthropic Claude. This is a governance decision, not a technical blocker. | Both | Policy compliance, cost management | Decision needed |

**Priority reference:**
- P1 — Blockers for basic reliable operation
- P2 — Required for security and governance compliance
- P3 — Required for full feature completeness

---

## 3. Hosting Options — Analysis

| Option | Provider | Setup effort | Backend dependency | Recommendation |
|---|---|---|---|---|
| A — GCP Cloud Run | GCP (Phygtl org) | Medium — requires GCP access and Dockerfile | High — Backend must provision | Best long-term if GCP access granted |
| **B — Independent VPS** | DigitalOcean / Render / Railway | **Low — one-click Ubuntu + hermes-deploy CLI** | **None — Data & AI controls entirely** | **Best short-term, no Backend dependency** |
| C — Supabase Edge Functions | Supabase | N/A — incompatible architecture | N/A | ❌ Not viable |

> **Supabase Edge Functions were evaluated and rejected.** They are stateless, have a 2-minute execution timeout, run on Deno (not Python), and have no persistent filesystem. Hermes Agent requires all of these. This path is architecturally incompatible.

### Recommended path — Option B (Independent VPS)

If Backend cannot grant GCP access in the short term, Data & AI should provision an independent VPS immediately. This removes the Backend dependency entirely and allows Argus to become fully operational without waiting.

**Setup steps (Data & AI executes independently):**

1. Provision Ubuntu 22.04 VPS on DigitalOcean, Render, or Railway (~$6–12/month)
2. Run the hermes-deploy one-line installer:
   ```bash
   curl -sSL https://raw.githubusercontent.com/unrealandychan/Hermes-Agent-Cloud/main/cli/install.sh | bash
   ```
3. Run `hermes-deploy` and follow interactive prompts (cloud, region, API keys)
4. Copy `lana_argus` profile from WSL2 to the VPS via SSH
5. Configure the public URL in Slack app Event Subscriptions
6. Test bidirectional @Argus interaction in `#11-analytics`

> The only Backend items still required in parallel are the dedicated Supabase service role key (P1) and GitHub PAT approval (P2).

---

## 4. Integration Requirements

### 4.1 Slack

| Item | Detail | Owner | Status |
|---|---|---|---|
| Bot token | `SLACK_BOT_TOKEN` in `.env` and `config.yaml`. Already configured. | Data & AI | ✅ Done |
| Channel | `#11-analytics` (C045W0RUGQ2). Bot invited to channel. | Data & AI | ✅ Done |
| Outbound messaging | `curl POST` to `chat.postMessage`. Working. | Data & AI | ✅ Done |
| App approval | Workspace admin approved Argus app install. | Backend | ✅ Done |
| Public URL | Required for Slack Event Subscriptions (inbound). | Backend | ⏳ Pending |
| Event Subscriptions | `app_mention` + `message.channels` events. Configured in api.slack.com/apps. | Data & AI | 🔴 Blocked on URL |

### 4.2 Supabase

| Item | Detail | Owner | Status |
|---|---|---|---|
| Dev environment | Connected via personal credentials. Working for governance checks. | Data & AI | ✅ Active |
| Production service role key | Dedicated read-only key for Argus. Must not be a personal key. | Backend | ⏳ Pending |
| Production URL | `SUPABASE_URL` for production project. | Backend | ⏳ Pending |
| RLS compliance | Argus queries via service role (bypasses RLS). Read-only scope mitigates risk. | Both | Confirm |

### 4.3 GitHub

| Item | Detail | Owner | Status |
|---|---|---|---|
| Data & AI push access | Daniel's personal PAT. Write access to Lana-Backend confirmed. | Data & AI | ✅ Active |
| Argus fine-grained PAT | `Hermes_Token_AI_Governance` — pending org admin approval. Scoped to `docs/` only. | Backend | ⏳ Pending approval |
| PR automation script | `open_governance_pr.py` — written and ready. Blocked on PAT approval. | Data & AI | 🔴 Blocked on PAT |

### 4.4 Google Sheets

| Item | Detail | Owner | Status |
|---|---|---|---|
| Service account | `lana-argus-sheets` in GCP. Currently under personal Google account. | Data & AI | ✅ Working |
| Spreadsheet sync | Governance artifacts sync to shared Google Sheet automatically. | Data & AI | ✅ Active |
| GCP org transfer | Service account should be moved to Phygtl GCP org for continuity. | Backend | ⏳ Pending confirmation |

---

## 5. Security Baseline

The following security requirements apply to any corporate agent deployed under this pattern:

- **One service role key per agent** — never share credentials between agents or with human accounts.
- **Read-only database access by default** — write access requires explicit justification and approval.
- **No PII in agent output channels** — Argus enforces this in SOUL.md. Any agent posting to shared Slack channels must follow the same rule.
- **Secrets in .env files only** — never hardcoded in SOUL.md, scripts, or committed to GitHub.
- **Agent LLM policy** — to be defined. Argus uses Anthropic Claude. A company-wide directive on agent LLM choice (Vertex AI / Gemini vs third-party) is needed before scaling to multiple agents.
- **Audit trail** — Argus daily reports serve as the audit log for governance actions. Future agents should follow the same pattern.

---

## 6. Questions for Backend

The following questions must be resolved with Backend to unblock the remaining P1 and P2 items.

| # | Topic | Question |
|---|---|---|
| Q1 | GCP access | Does Data & AI have permission to create resources (VMs, Cloud Run services, service accounts) in the Phygtl GCP organization? Or does all GCP provisioning go through Backend? |
| Q2 | Argus GCP project | Is the `lana-argus-sheets` GCP project under the Phygtl org, or under a personal Google account? If personal, can it be transferred? |
| Q3 | Independent VPS | If Data & AI cannot create GCP resources independently, is there an objection to running the Argus agent on an independent VPS (e.g. DigitalOcean) that Data & AI controls? This removes Backend from the hosting dependency entirely. |
| Q4 | Supabase service role key | Can you create a dedicated read-only service role key for Argus on the production Supabase project? It should be separate from any personal or existing keys and scoped to public schema read access only. |
| Q5 | GitHub PAT approval | The `Hermes_Token_AI_Governance` fine-grained PAT is pending org admin approval. Can you approve it? It needs read/write access to `Lana-Backend/docs` only. |
| Q6 | LLM policy | Is there a directive that internal agents must use Vertex AI / Gemini? Argus currently uses Anthropic Claude (API). We need to know if this is a compliance issue or if agent LLM choice is left to Data & AI. |

> Data & AI can proceed with Option B (independent VPS) independently while Q1–Q3 are being resolved. Q4 (Supabase key) and Q5 (GitHub PAT) are the only Backend items that block agent functionality regardless of hosting choice.

---

*Lana · Phygtl, Inc. · Internal document · Not for external distribution · v1.0 · July 2026*
