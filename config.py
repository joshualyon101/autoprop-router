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
    state_poll_seconds: float = Field(default=10.0, ge=2.0)
    max_state_age_seconds: float = Field(default=20.0, ge=2.0)
    request_timeout_seconds: float = Field(default=4.0, ge=0.5)
    sqlite_path: str = "autoprop_router.sqlite3"
    account_config_path: str = "accounts.json"
    default_execution_symbol: str = ""  # e.g. MNQU6; intentionally not hard-coded
    mnq_point_value: float = 2.0
    mnq_tick_size: float = 0.25
    modeled_round_trip_cost: float = 2.90
    consistency_min_profit_room: float = 50.0
    use_crosstrade_position_gate: bool = True
    use_crosstrade_max_positions_gate: bool = False
    auto_discovery: bool = True

    # Live rollout controls.
    canary_account_id: str = ""
    canary_max_qty: int = 1
    live_max_qty_per_account: int = 0  # 0 = use calculated qty
    execution_require_bracket: bool = True
    use_native_atm: bool = True
    fundednext_default_model: str = "Legacy"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            crosstrade_base_url=os.getenv("CROSSTRADE_BASE_URL", "https://app.crosstrade.io"),
            crosstrade_token=os.getenv("CROSSTRADE_TOKEN", ""),
            webhook_token=os.getenv("AUTOPROP_WEBHOOK_TOKEN", ""),
            execution_mode=os.getenv("AUTOPROP_EXECUTION_MODE", "shadow").lower(),
            live_arm=os.getenv("AUTOPROP_LIVE_ARM", ""),
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
            canary_account_id=os.getenv("CANARY_ACCOUNT_ID", ""),
            canary_max_qty=int(os.getenv("CANARY_MAX_QTY", "1")),
            live_max_qty_per_account=int(os.getenv("LIVE_MAX_QTY_PER_ACCOUNT", "0")),
            execution_require_bracket=os.getenv("EXECUTION_REQUIRE_BRACKET", "true").lower() == "true",
            use_native_atm=os.getenv("USE_NATIVE_ATM", "true").lower() == "true",
            fundednext_default_model=os.getenv("FUNDEDNEXT_DEFAULT_MODEL", "Legacy"),
        )

    @property
    def live_enabled(self) -> bool:
        return (
            self.execution_mode in {"canary", "live"}
            and self.live_arm == "I_UNDERSTAND_LIVE_ORDERS"
        )

    @property
    def canary_enabled(self) -> bool:
        return self.live_enabled and self.execution_mode == "canary"

    def load_accounts(self) -> list[AccountConfig]:
        path = Path(self.account_config_path)
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        return [AccountConfig.model_validate(x) for x in data.get("accounts", [])]
