from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, model_validator


class AccountPhase(str, Enum):
    CHALLENGE = "challenge"
    FUNDED = "funded"
    PERSONAL = "personal"


class DrawdownType(str, Enum):
    STATIC = "static"
    EOD_TRAIL_TO_LOCK = "eod_trail_to_lock"
    LIVE_TRAIL_TO_LOCK = "live_trail_to_lock"
    EOD_TRAIL_NO_PREPASS_LOCK = "eod_trail_no_prepass_lock"
    LIVE_TRAIL_NO_PREPASS_LOCK = "live_trail_no_prepass_lock"


class RiskProfile(str, Enum):
    STANDARD = "Standard"
    AGGRESSIVE = "Aggressive"


class AccountConfig(BaseModel):
    id: str
    # Stable Tradovate/CrossTrade account ID. Prefer this for identity matching.
    crosstrade_account_id: int | None = None
    account_name: str
    firm: str
    program: str
    phase: AccountPhase
    starting_balance: float = Field(gt=0)
    max_loss: float = Field(ge=0)
    profit_target: float = Field(default=0, ge=0)
    max_micros: int = Field(gt=0, le=500)
    drawdown_type: DrawdownType
    lock_offset: float = 0.0
    consistency_pct: float | None = Field(default=None, gt=0, le=100)
    daily_loss_limit: float | None = Field(default=None, gt=0)
    risk_profile: RiskProfile = RiskProfile.STANDARD

    # Prop trading-day boundaries. Default fits the current futures farm:
    # session day starts 6:00 PM ET; EOD MLL snapshot is taken 6:05 PM ET,
    # after the existing 4:00/4:10 PM flatten window and before Asia trading.
    # Verify per firm/account before rules_verified=true.
    trading_day_start_et: str = "18:00"
    eod_snapshot_time_et: str | None = "18:05"

    enabled: bool = False
    rules_verified: bool = False

    # Optional persisted bootstrap. Once live state is established, the router
    # maintains these values automatically.
    bootstrap_mll_floor: float | None = None
    bootstrap_peak_eod_balance: float | None = None
    bootstrap_live_high_water: float | None = None

    # Dynamic registry metadata.
    auto_discovered: bool = False
    profile_source: str = "static"
    risk_ready: bool = False

    @property
    def lock_level(self) -> float:
        return self.starting_balance + self.lock_offset


class AccountRuntime(BaseModel):
    account_id: str
    account_name: str
    balance: float | None = None
    net_liq: float | None = None
    mll_floor: float | None = None
    cushion: float | None = None
    peak_eod_balance: float | None = None
    live_high_water: float | None = None
    mll_locked: bool = False
    positions: list[dict[str, Any]] = Field(default_factory=list)
    working_orders: list[dict[str, Any]] = Field(default_factory=list)
    source_updated_at: datetime | None = None
    cache_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    day_start_balance: float | None = None
    realized_today: float = 0.0
    largest_winning_day: float = 0.0
    trading_day_key: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        for p in self.positions:
            qty = p.get("quantity", p.get("qty", p.get("netPos", 0)))
            try:
                if abs(float(qty or 0)) > 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    @property
    def has_working_orders(self) -> bool:
        return len(self.working_orders) > 0


class SignalEvent(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    FLATTEN = "FLATTEN"
    STOP_MOVE = "STOP_MOVE"


class TradeSignal(BaseModel):
    trade_id: str = Field(min_length=3, max_length=128)
    event: SignalEvent
    engine: str = Field(min_length=1, max_length=64)
    direction: str | None = None
    instrument: str = "MNQ1!"
    execution_symbol: str | None = None

    entry: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None

    # Preferred: Pine sends the exact setup-specific risk-per-contract it used.
    contract_risk_dollars: float | None = Field(default=None, gt=0)

    # Optional account-independent strategy context for consistency/progress parity.
    note: str | None = None
    emitted_at_ms: int | None = None

    # Used by Fusion's consistency size-first / target-clipping logic.
    min_runner_qty: int = Field(default=2, ge=1, le=100)

    # Optional broker-native management. False keeps both ATM tiers fixed.
    breakeven_after_tp1: bool = False
    breakeven_offset_ticks: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def validate_entry(self):
        if self.event == SignalEvent.ENTRY:
            if not self.direction:
                raise ValueError("direction is required for ENTRY")
            d = self.direction.upper()
            if d not in {"LONG", "SHORT", "BUY", "SELL"}:
                raise ValueError("direction must be LONG/SHORT/BUY/SELL")
            self.direction = d
            if self.contract_risk_dollars is None and (self.entry is None or self.stop is None):
                raise ValueError("ENTRY requires contract_risk_dollars, or both entry and stop")
        return self


class RouteDecision(BaseModel):
    account_id: str
    account_name: str
    eligible: bool
    reason: str
    quantity: int = 0
    risk_budget: float = 0.0
    contract_risk: float = 0.0
    cushion: float | None = None
    mll_floor: float | None = None
    cache_age_ms: float | None = None
    routed_tp1: float | None = None
    routed_tp2: float | None = None
    consistency_room: float | None = None
    execution_result: dict[str, Any] | None = None


class RouteResponse(BaseModel):
    trade_id: str
    mode: str
    router_processing_ms: float
    decisions: list[RouteDecision]
