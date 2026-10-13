# n8n Automation: Lead Enrichment Workflow

`lead_enrichment_workflow.json` is an importable n8n workflow that takes a company URL, triggers asynchronous enrichment through the REST API, polls until the lead is scored, and routes it by ICP fit.

```
Webhook ─► Enrich Lead ─► Wait (10s) ─► Poll Lead Status ─► Check Status
                             ▲                                  │
                             └────────── PENDING (≤ 11 polls) ──┤
                                                                ├─ PROCESSED ─► Is ICP Qualified? ─┬─ yes ─► Format Sales Notification
                                                                │                                  └─ no  ─► Route to Nurture
                                                                ├─ FAILED ────► Report Failure
                                                                └─ timeout ───► Report Timeout
```

## Start the stack

```bash
docker compose up -d --build
```

n8n starts only after the `app` healthcheck passes. The editor is bound to loopback: <http://localhost:5678>. Workflows and credentials persist in the `n8n_data` volume.

## Import the workflow

1. Open <http://localhost:5678> and create the owner account on first launch.
2. **Workflows → ⋯ (top right) → Import from File…** and select `automation/n8n/lead_enrichment_workflow.json`.
3. Click **Save**, then toggle **Active** to register the production webhook URL.

## Network routing

n8n and the API share the Compose network, so the workflow addresses the API by service name: `http://app:8000`. Do **not** use `localhost` inside a node. That resolves to the n8n container itself. From the host, the API stays reachable at `http://localhost:8000`.

## Trigger payload

`POST` to the webhook (use `/webhook-test/lead-enrichment` while the editor is listening, `/webhook/lead-enrichment` once the workflow is Active):

```bash
curl -X POST http://localhost:5678/webhook/lead-enrichment \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://linear.app"}'
```

| Field   | Type    | Required | Description                                             |
|---------|---------|----------|---------------------------------------------------------|
| `url`   | string  | yes      | Public http(s) company URL; validated by the API.       |
| `force` | boolean | no       | Re-enrich a URL that was already processed (default `false`). |

The webhook answers immediately (`responseMode: onReceived`); enrichment continues in the background. To feed a list of URLs on a timer instead, replace the Webhook node with a Schedule Trigger followed by a node that emits one item per URL, and change the `url` expression in **Enrich Lead** to `$json.url`.

## Nodes

| Node | Purpose |
|------|---------|
| **Webhook: Company URL** | Receives `{"url": "...", "force": false}`; the body is available as `$json.body`. |
| **Enrich Lead** | `POST http://app:8000/api/v1/enrich`. Returns `202` with `lead_id` for a new lead, or `200` with the existing `lead_id` if the URL was already processed. |
| **Wait for Enrichment** | 10-second pause while the API scrapes and scores in the background. |
| **Poll Lead Status** | `GET http://app:8000/api/v1/leads/{lead_id}`. The id is read from **Enrich Lead** (not `$json`) so it survives the polling loop. |
| **Check Status** | Switch on `status`: `PROCESSED` → qualification, `FAILED` → failure report, `PENDING` → back to the Wait node, capped at 11 polls (~2 min) before the timeout branch. |
| **Is ICP Qualified?** | True when `fit_tier` starts with `Tier 1` **or** `fit_score >= 75`. The API tiers are `Tier 1 (High)`, `Tier 2 (Medium)`, `Tier 3 (Low)` and `Disqualified`, hence `startsWith`. |
| **Format Sales Notification** | Mock action: builds a Slack/Telegram-style `message` plus a `lead` object. |
| **Route to Nurture** | Mock action for unqualified leads. |
| **Report Failure / Report Timeout** | Mock alerts; `error_message` from the API is surfaced on failure. |

## Going to production

- Replace the mock **Set** nodes with real Slack, Telegram, or CRM nodes and attach their credentials in n8n. Use the `message` and `lead` fields they emit.
- Tune the Wait duration and the poll cap (`$runIndex < 11` in **Check Status**) to your typical scrape-and-score latency.
- The Compose file pins no n8n version (`latest`). Pin a tag for reproducible deployments.
- `N8N_SECURE_COOKIE=false` is for local HTTP only. Put n8n behind HTTPS and remove it in any shared environment.
- The webhook is unauthenticated by default. Add Header Auth to the Webhook node before exposing it beyond localhost.
