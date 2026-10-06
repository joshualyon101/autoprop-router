# Shadow deployment and Pine alert setup

## Railway configuration

Preserve the existing webhook token and account configuration. Use these explicit values in Railway; existing environment variables override package defaults:

```text
AUTOPROP_EXECUTION_MODE=shadow
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false
SHADOW_BROKER_READS_ENABLED=false
SHADOW_SQLITE_PATH=/data/autoprop_router_shadow.sqlite3
AUTOPROP_LIVE_ARM=
AUTOPROP_FULL_SCALE_ARM=
```

Keep the shadow database on persistent storage and separate from the live Router database. Deploy the contents of the ZIP's top-level folder, which contains the Dockerfile and railway.toml. This delivered package has not been deployed on your behalf.

After deployment, `/health` must report version `1.2.8.14-bounded-state-fallback-rc1` and:

```json
{
  "execution_mode": "shadow",
  "router_role": "shadow_observer",
  "production_execution_path": "direct_tradingview_to_crosstrade",
  "shadow_intake_ready": true,
  "shadow_broker_reads_enabled": false,
  "broker_mutation_capable": false,
  "broker_mutation_armed": false,
  "broker_mutation_firewall": true
}
```

## Pine setup

Install `AutoProp_Fusion_Trade_Navigator_v1.2.5_FINAL.pine` on standard one-minute CME_MINI:MNQ1! candles. Save/compile the final source in TradingView. The selected rule is always active; there is no breadth switch to enable. The default account remains a 50K Challenge example. Set the actual account mode, starting balance, loss allowance, contract cap, profit target, consistency rule, drawdown type, and lock offset for the intended account.

For a historical check, keep **Balance source = Backtest / Strategy Tester** and **Enable webhook messages = Off**. Match **Properties > Initial capital** to **Inputs > Starting balance**.

For Direct production, use **Balance source = Live / Manual Account State** and populate the current closed account balance and actual minimum balance / failure floor. These values are supplied by the Pine alert snapshot and do not automatically refresh from CrossTrade. A shadow Router does not supply account state to the Direct path. The script blocks Direct automation when Balance source is Backtest.

## Alert snapshots

Create or replace alerts while destination accounts are flat and no ASW limit is pending. Retire the prior alert before its replacement runs. Recreate every affected alert after changing script or inputs; changing the chart does not update an existing server alert.

### Direct alert

- Execution Alert Route: **CrossTrade Direct**.
- Enable webhook messages: **On** after filling the destination account and webhook key.
- Balance source: **Live / Manual Account State**.
- Webhook URL: your CrossTrade webhook.
- Condition: **Order fills and alert() function calls**.
- Message: `{{strategy.order.alert_message}}`.

One Direct alert addresses one destination. Use the existing verified Direct-account or copier arrangement for other accounts; Router discovery does not add them to this Direct execution path.

### Canonical shadow alert

- Execution Alert Route: **Railway Router**.
- Enable webhook messages: **On**.
- Use the same account/sizing profile as the Direct snapshot chosen for observation.
- Webhook URL: `https://YOUR-RAILWAY-DOMAIN/webhook/tradingview-shadow/AUTOPROP_WEBHOOK_TOKEN`.
- Condition: **Order fills and alert() function calls**.
- Message: `{{strategy.order.alert_message}}`.

Use one canonical Shadow feed. Do not duplicate it per Direct account. The `/webhook/tradingview/...` live Router endpoint is rejected in shadow mode.

## Continue observing trades

Review the shadow inbox at `/admin/webhook-inbox/AUTOPROP_WEBHOOK_TOKEN`. A processed event should show `SHADOW_OBSERVED` and `broker_mutation_performed=false`. For the final Pine's ORG entries, check the supplied `Q_BASE`, `QTY`, `RG_HALF`, and the `regime_sizing` diagnostic.

Compare actual orders, fills, protective stops, and management in CrossTrade/Tradovate with their intended Direct plans. With broker reads disabled, successful shadow observations do not verify broker execution. Keep the existing independent risk limits and scheduled prop cutoffs in CrossTrade Account Manager. Direct Core management uses its existing native ATM plus target-fill breakeven behavior; it does not reproduce every Pine modeled management step.

Remain in shadow mode for this release. A future switch to Router execution needs a separate audit of destination sizing, including the new ORG reduction; the source quantity audit added here does not implement live per-account sizing.
