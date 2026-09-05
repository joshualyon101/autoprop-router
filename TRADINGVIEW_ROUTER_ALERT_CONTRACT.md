# AutoProp TradingView -> Router production alert contract

The Router accepts structured JSON and recognized AutoProp pipe messages. Plain-English messages are rejected intentionally.

Preferred JSON events:

- `ENTRY` / `ENTRY_FILL`: direction, entry, stop, tp1, optional tp2, source_qty, tp1_qty, runner_qty.
- `PARTIAL_EXIT` / `TP1_FILL`: optional exit_qty plus current stop.
- `STOP_MOVE` / `STOP_MOVE_BE` / `STOP_MOVE_HALF_RISK`: new stop.
- `EXIT` / `TP2` / `STOP_FILL` / `RUNNER_STOP_FILL`: full strategy exit.
- `FLATTEN` / `SESSION_EXIT` / `REQUIRED_FLAT_EXIT`: force flat.

Native ATM mode uses only ENTRY plus FLATTEN for broker mutation. Normal TradingView exit/stop events are deliberately ignored so they cannot double-close a broker-owned ATM.

TV-managed mode uses a broker-hosted stop on entry, then TradingView events for partial exits, stop replacements, and final exits. Do not select TV-managed mode until the current AutoProp ICT Fusion alert messages have been audited end-to-end.
