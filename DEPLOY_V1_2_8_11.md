# AutoProp Router v1.2.8.11 shadow canary deployment

This release is a shadow observer by default. Production orders must go directly from
TradingView to CrossTrade while this release is being evaluated.

Use the separately delivered
`AutoProp_ICT_Fusion_v1.2.4A_DIRECT_SHADOW_ORG_BREADTH_RC4.pine`. It preserves the RC3
direct/shadow routing fixes and adds the optional ORG breadth sizing rule.
Compile/save it in TradingView before replacing any alert.
Read `ORG_BREADTH_RC1.md` for the new rule, alert fields, and scope.

## 1. Railway variables

Set or update these variables before deploying:

```text
AUTOPROP_EXECUTION_MODE=shadow
TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false
SHADOW_BROKER_READS_ENABLED=false
SHADOW_SQLITE_PATH=/data/autoprop_router_shadow.sqlite3
```

Keep the existing `AUTOPROP_WEBHOOK_TOKEN`, account configuration, and formula settings.
The CrossTrade token may remain configured, but shadow mode does not use it while
`SHADOW_BROKER_READS_ENABLED=false`. Live/full-scale arm values are ignored by shadow mode;
blanking them is additional operator protection.

Do not turn `TRADINGVIEW_ALERT_CONTRACT_VERIFIED` on for this shadow deployment.

## 2. Verify the deployed role

Open `/health`. Required fields are:

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

Stop if any of those values differ.

## 3. TradingView alerts

The supplied Pine build has one mutually exclusive `Execution Alert Route` input. Create
two separate TradingView alert snapshots from otherwise identical inputs:

1. **Production Direct alert**
   - `Execution Alert Route = CrossTrade Direct`
   - `Balance source = Live / Manual Account State`
   - Webhook URL: the CrossTrade webhook URL
   - Condition: **Order fills and alert() function calls**
   - Message: `{{strategy.order.alert_message}}`
2. **Router Shadow alert**
   - Change only `Execution Alert Route = Railway Router`
   - Keep `Balance source = Live / Manual Account State` and every other strategy input
     identical to the Direct snapshot
   - Webhook URL:
     `https://YOUR-RAILWAY-DOMAIN/webhook/tradingview-shadow/AUTOPROP_WEBHOOK_TOKEN`
   - Condition: **Order fills and alert() function calls**
   - Message: `{{strategy.order.alert_message}}`

The new **Half ORG on high breadth** input defaults to Off. Enable it only for
the chosen canary account's Direct alert and the one canonical Shadow alert
when those charts share the same sizing profile. The untouched Direct account
remains a reference. Any change requires a new alert snapshot.

TradingView freezes the script and inputs when an alert is created, so switching the chart
route after the Direct alert is created does not alter that existing alert.

Use exactly one canonical Shadow alert. Per-account Direct alerts necessarily have different
account/manual-state inputs; duplicating all of them into Shadow would overcount one strategy
event as multiple eight-account observations. The canonical Shadow feed audits the selected
profile's alert contract, not every distinct per-account Direct sizing profile.

Disable/delete the old alert that points to `/webhook/tradingview/...`. This release rejects
that live-router endpoint while in shadow mode.

Changing `Enable webhook messages` or any chart input does not change an already-running server
alert. To stop it, disable/delete the alert. Recreate an alert only while every destination is
flat and no ASW limit is pending, and retire the old snapshot before enabling its replacement.

## 4. What shadow mode records

Shadow mode records parsed event timing, engine, side, exact plan geometry, intended enabled
accounts, market pulses, stop-stage instructions, and modeled exit/control events. Results are
available at `/admin/webhook-inbox/AUTOPROP_WEBHOOK_TOKEN` and contain:

```text
status=SHADOW_OBSERVED
action=WOULD_...
broker_mutation_performed=false
```

With broker reads disabled, this release deliberately does not claim to simulate live fills or
recalculate per-account quantities from current balances. Its health capability is therefore
reported as `alert_plan_observer`.

## 5. Direct-route account limitation

One Pine Direct alert names one CrossTrade destination account. Eight-account fanout therefore
requires either one Direct alert per destination or a separately verified CrossTrade copier.
Router auto-discovery/onboarding does not add accounts to the Direct path while the Router is in
shadow mode. A newly purchased challenge must be enrolled in that Direct/copy path before it can
receive production trades during this evaluation period.

The Pine Direct route uses the alert snapshot's manual balance and MLL/failure-floor inputs. Those
values do not refresh themselves from CrossTrade, and editing the chart does not update an alert
that already exists. Use conservative values plus independent CrossTrade/Account Manager risk and
cutoff controls, and recreate each Direct alert whenever its balance, MLL floor, rules, or other
inputs change. Do not treat the temporary Direct route as permanent zero-touch account-state
automation.

Use dedicated MNQ accounts with no manual or sibling-strategy MNQ orders during this Direct test.
Direct safety FLATTEN is intentionally account/instrument-wide, and Silver `CANCELANDBRACKET`
cancels existing MNQ working orders before rebuilding protection. Direct ORG/Silver signals are
triggered by TradingView's modeled limit fills but sent to the broker as MARKET orders, so a fast
market can produce slippage or rejection while the command retains the planned absolute stop and
target. Keep the independent Account Manager cutoff/flatten protection required by the Pine header.

## 6. Core incident fix retained for a later canary

The live code path now distinguishes broker read uncertainty from affirmative loss of
protection. A lifecycle timeout or delayed final CHANGE report retains the accepted native ATM,
opens the new-entry circuit, and retries without resending or flattening. Explicitly unsafe
broker evidence and intentional EXIT/HARD_FLAT controls keep their fail-safe behavior.

Do not change this deployment from shadow to live as an informal test. A future canary should be
a deliberate deployment after the shadow sample is reviewed.
