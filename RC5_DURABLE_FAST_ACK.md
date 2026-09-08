# AutoProp Router v1.2.6 — Exact Parity Production RC5
## Durable TradingView Fast-ACK Hotfix

### Trigger
The first live DWC entry alert reached the Router webhook but TradingView reported:
`Webhook delivery failed — request took too long and timed out`.
CrossTrade received no signal. v1.2.5 performed seven-account broker state/risk/execution work synchronously inside the TradingView HTTP request.

### Architecture change
RC5 changes only the webhook transport/orchestration seam:

1. Authenticate and parse the TradingView alert.
2. Verify the Router is configured and the TradingView alert contract gate is armed.
3. Durably insert the validated raw event into SQLite `webhook_inbox`.
4. Return HTTP 200 immediately.
5. A single ordered background worker claims the durable event and performs the existing exact-parity CrossTrade route.

The event is committed to SQLite before acknowledgement. A process restart requeues an event that was left in `PROCESSING` state. Same-day identical TradingView retries dedupe at the inbox using the stable alert hash plus New York trading date.

### New diagnostic endpoint
`GET /admin/webhook-inbox/<token>`

Shows recent durable events and statuses: `PENDING`, `PROCESSING`, `DONE`, `FAILED`, including the route result/error when available.

### Logic parity
No changes were made to:
- five-engine Pine alert parsing/geometry
- formula parity
- account allocation/risk sizing
- CrossTrade execution/protection
- fill proof
- management
- ORG re-entry
- state/ledger calculations
- rate-limit hardening

Only `main.py`, `store.py`, and `version.py` changed from v1.2.5 RC4.
