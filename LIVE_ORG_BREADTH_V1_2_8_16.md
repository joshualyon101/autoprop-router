# Live ORG breadth companion — v1.2.8.16 RC1

## Why this companion is required

The Pine update emits its completed five-session breadth reading and frozen ORG
half-size decision. The v15 live allocators did not consume that decision when sizing
destination accounts. v16 applies the decision to each account's own final executable
quantity; Pine's absolute contract count does not replace destination account sizing.

For example, a destination capped at five contracts submits two when the half flag is
active. A destination that can validly take only one remains at one. A destination with
zero eligible quantity remains blocked. All other engines retain existing sizing.

## Metadata contract

ORG ENTRY alerts from the matching Pine carry:

- `RG_VER=EW5_B50`
- `RG_POLICY=ALWAYS_ON`
- `RG_EN=1`
- `B5`: completed five-session NDXE return minus NDX return, in percentage points
- `RG_HALF=0` or `1`: the decision frozen for this setup
- `Q_BASE`: positive Pine executable quantity before the regime adjustment
- `QTY`: positive Pine executable quantity after the regime adjustment

The locked threshold is strictly greater than +0.50 percentage points. Missing breadth
(`NaN` or `na`) retains normal size and requires the half flag to be zero. Valid source
quantities must satisfy `QTY = max(1, floor(Q_BASE / 2))` for half size, or `QTY = Q_BASE`
for normal size. Unknown contracts/policies, incomplete or duplicate fields, invalid
flags, nonfinite readings, fractional quantities, or clear flag/reading disagreements
block live allocation.

Pine encodes B5 to eight decimal places. Within 5.001e-9 of the threshold, the router
preserves the reported frozen flag because the rounded reading cannot reconstruct the
original strict comparison. It never recomputes a new index reading or flips a deferred
setup's decision. This validates an authenticated Pine report; it does not independently
verify the source index feed.

Legacy alerts without any `RG_*` or `Q_BASE` markers keep existing behavior. A legacy
B5 field alone does not activate the new contract. Shadow parsing retains malformed or
OPTIONAL metadata for observation instead of raising a live-only contract error.

## Allocation and durable state

Normal allocation performs all existing eligibility, fresh-state, drawdown, risk,
contract-cap, and consistency checks, then applies ORG half size before recalculating
its consistency target. Bounded fallback applies half size after its existing
conservative risk-budget floor and contract cap; it does not fabricate a ledger or new
consistency target. The raw metadata survives CanonicalPlan JSON persistence/replay.

A separate entry fan-out fix makes the disabled-account preflight result match the
four-field result expected by the live route. Previously a disabled destination could
raise an unpacking exception before any enabled destination order was submitted.
Disabled accounts are now skipped while eligible accounts continue.

The v15 readback recovery, diagnostics, broker protection, entry circuit ownership,
fallback wave/duration limits, and arm gates are retained. This release introduces no
blind reset, timed order replay, or new broker command.

## Installation

The companion ZIP is a changed-files patch for the deployed v1.2.8.15 router. Upload the
files inside `changed-files/` into the repository root beside `main.py`, overwriting
matching files. Do not upload a containing `changed-files` directory. The new test and
Markdown files can remain in the root; they do not execute in production. Retain all
other v15 files, including `entry_recovery.py`, plus the existing Railway variables and
persistent SQLite volume.

After Railway redeploys, `/health` must report
`1.2.8.16-live-org-breadth-rc1` and retain `readback_auto_recovery_enabled: true`.
Check the existing configuration/alert-contract/arm flags against your intended live
setup. Do not clear a circuit merely to satisfy a version check; a timeout circuit
still needs the existing recovery guards to succeed.

The Pine file belongs in TradingView's Pine Editor, not in the router repository.
Use standard MNQ1! one-minute candles. Reapply your account/engine inputs, select
Railway Router, and enable webhook messages. Backtest / Strategy Tester balance mode
is valid in Router mode because the router owns live destination account state.

Compile/Add to chart before replacing alerts. Stop the old strategy alerts, then create
replacement alerts from the updated strategy with "Order fills and alert() function
calls", Message `{{strategy.order.alert_message}}`, and the existing authenticated
router webhook URL. Avoid keeping old and replacement strategy alerts active together.
Changing the chart script does not update an existing TradingView alert snapshot.

## Pine scope and evidence limits

The Pine candidate is based on the newer no-daily-stops RC1 source and preserves the
uploaded RC4 Silver Direct protections. It removes the portfolio realized-loss entry
lock and global daily count gate, restores the locked ORG breadth policy, changes four
trade-map anchors to timestamps, and expires obsolete candidate identity keys after
their original eligibility window. Per-engine lifecycle/session gates and account
safeguards remain.

The available logs show separate router transport/webhook failures and TradingView
calculation-error stops. The screenshots do not identify the exact Pine exception or
the cause of the original CrossTrade positions ReadTimeout. The runtime changes address
identified source vulnerabilities; they are not proof of the historical exception.

Python models verified 360,000 native processing pulses and 357,617 candidate decisions
against the prior unbounded key behavior, including expiry boundaries, masked insertion
order, and midnight transitions. Static checks cover balanced source delimiters, all six
ORG alert paths, paired key/stamp mutation, timestamp trade-map state, HUD bounds, and
unchanged Silver Direct helpers. These checks are not a TradingView compiler or a market
backtest. TradingView compilation and chart behavior must be checked during installation.

Router regression: 475 passed, 1 deselected in the live-configured flat-layout suite
(31 modules), plus the deselected default-shadow safety case run independently: 1 passed.
Total verified cases: 476, including 56 new v16 cases. The new cases cover account types,
odd/even and one-contract caps, consistency ceilings/target compression, missing data,
metadata/rounding validation, canonical JSON persistence, normal and fallback long/short
live route fan-out to mocked PLACE, mixed disabled destinations, durable accepted
quantities, malformed metadata producing no PLACE, and reentry requiring proven
prior destination stop fills. Existing FastAPI/Starlette deprecation warnings remain.
Historical tests that import an unavailable `app.*` layout and `test_production_rc.py`
were excluded; they are not part of this flat-layout verification. No real broker orders
were sent during verification.
