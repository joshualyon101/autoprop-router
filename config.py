from __future__ import annotations

import json
import os
from pathlib import Path
from pydantic import BaseModel, Field
from models import AccountConfig


class Settings(BaseModel):
    crosstrade_base_url: str = "https://app.crosstrade.io"
    crosstrade_token: str = ""
    webhook_token: str = ""
    execution_mode: str = "shadow"  # shadow | canary | live
    live_arm: str = ""
    full_scale_arm: str = ""
    alert_contract_verified: bool = False

    state_poll_seconds: float = Field(default=10.0, ge=2.0)
    max_state_age_seconds: float = Field(default=20.0, ge=2.0)
    request_timeout_seconds: float = Field(default=4.0, ge=0.5)
    sqlite_path: str = "autoprop_router.sqlite3"
    account_config_path: str = "accounts.json"
    default_execution_symbol: str = ""
    mnq_point_value: float = 2.0
    mnq_tick_size: float = 0.25
    modeled_round_trip_cost: float = 2.90
    consistency_min_profit_room: float = 50.0
    use_crosstrade_position_gate: bool = True
    use_crosstrade_max_positions_gate: bool = False
    auto_discovery: bool = True
    fundednext_default_model: str = "Legacy"

    # Full-scale production: 0 means no extra Router quantity cap. Firm account
    # max_micros and AutoProp's risk engine still apply per account.
    live_max_qty_per_account: int = 0

    # Execution management modes:
    # native_atm = Tradovate owns fixed stops, scale-out targets and OCO.
    # tv_managed = broker has a catastrophic stop; TradingView events manage exits.
    management_mode: str = "native_atm"
    native_atm_breakeven_after_tp1: bool = True
    execution_require_protective_stop: bool = True

    # Optional canary remains available, but production docs do not use it.
    canary_account_id: str = ""
    canary_max_qty: int = 1

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            crosstrade_base_url=os.getenv("CROSSTRADE_BASE_URL", "https://app.crosstrade.io"),
            crosstrade_token=os.getenv("CROSSTRADE_TOKEN", ""),
            webhook_token=os.getenv("AUTOPROP_WEBHOOK_TOKEN", ""),
            execution_mode=os.getenv("AUTOPROP_EXECUTION_MODE", "shadow").lower(),
            live_arm=os.getenv("AUTOPROP_LIVE_ARM", ""),
            full_scale_arm=os.getenv("AUTOPROP_FULL_SCALE_ARM", ""),
            alert_contract_verified=os.getenv("TRADINGVIEW_ALERT_CONTRACT_VERIFIED", "false").lower() == "true",
            state_poll_seconds=float(os.getenv("STATE_POLL_SECONDS", "10")),
            max_state_age_seconds=float(os.getenv("MAX_STATE_AGE_SECONDS", "20")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "4")),
            sqlite_path=os.getenv("SQLITE_PATH", "autoprop_router.sqlite3"),
            account_config_path=os.getenv("ACCOUNT_CONFIG_PATH", "accounts.json"),
            default_execution_symbol=os.getenv("DEFAULT_EXECUTION_SYMBOL", ""),
            mnq_point_value=float(os.getenv("MNQ_POINT_VALUE", "2.0")),
            mnq_tick_size=float(os.getenv("MNQ_TICK_SIZE", "0.25")),
            modeled_round_trip_cost=float(os.getenv("MODELED_ROUND_TRIP_COST", "2.90")),
            consistency_min_profit_room=float(os.getenv("CONSISTENCY_MIN_PROFIT_ROOM", "50.0")),
            use_crosstrade_position_gate=os.getenv("USE_CROSSTRADE_POSITION_GATE", "true").lower() == "true",
            use_crosstrade_max_positions_gate=os.getenv("USE_CROSSTRADE_MAX_POSITIONS_GATE", "false").lower() == "true",
            auto_discovery=os.getenv("AUTO_DISCOVERY", "true").lower() == "true",
            fundednext_default_model=os.getenv("FUNDEDNEXT_DEFAULT_MODEL", "Legacy"),
            live_max_qty_per_account=int(os.getenv("LIVE_MAX_QTY_PER_ACCOUNT", "0")),
            management_mode=os.getenv("AUTOPROP_MANAGEMENT_MODE", "native_atm").lower(),
            native_atm_breakeven_after_tp1=os.getenv("NATIVE_ATM_BREAKEVEN_AFTER_TP1", "true").lower() == "true",
            execution_require_protective_stop=os.getenv("EXECUTION_REQUIRE_PROTECTIVE_STOP", "true").lower() == "true",
            canary_account_id=os.getenv("CANARY_ACCOUNT_ID", ""),
            canary_max_qty=int(os.getenv("CANARY_MAX_QTY", "1")),
        )

    @property
    def full_scale_armed(self) -> bool:
        return self.full_scale_arm == "I_UNDERSTAND_FULL_SCALE"

    @property
    def base_live_armed(self) -> bool:
        return self.live_arm == "I_UNDERSTAND_LIVE_ORDERS" and self.alert_contract_verified

    @property
    def live_enabled(self) -> bool:
        if self.execution_mode == "canary":
            return self.base_live_armed and bool(self.canary_account_id)
        if self.execution_mode == "live":
            return self.base_live_armed and self.full_scale_armed
        return False

    @property
    def canary_enabled(self) -> bool:
        return self.live_enabled and self.execution_mode == "canary"

    def load_accounts(self) -> list[AccountConfig]:
        path = Path(self.account_config_path)
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        return [AccountConfig.model_validate(x) for x in data.get("accounts", [])]
