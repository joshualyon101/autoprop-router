from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

AccountType = Literal["challenge", "funded", "personal"]
Profile = Literal["standard", "aggressive"]
Drawdown = Literal["static", "eod", "live"]
Side = Literal["LONG", "SHORT"]


class AccountRule(BaseModel):
    account_id: str
    crosstrade_account: str
    enabled: bool = False
    rules_verified: bool = False
    account_type: AccountType
    profile: Profile = "standard"
    starting_balance: float = Field(gt=0)
    max_loss: float = Field(ge=0)
    max_contracts: int = Field(gt=0)
    drawdown: Drawdown = "eod"
    challenge_target: float = Field(default=0, ge=0)
    challenge_consistency_enabled: bool = False
    challenge_consistency_pct: float = Field(default=40.0, gt=0, le=100)
    funded_consistency_enabled: bool = False
    funded_daily_profit_cap: float = Field(default=0, ge=0)
    personal_risk_method: Literal["percent", "fixed"] = "fixed"
    personal_risk_value: float = Field(default=250.0, ge=0)
    # Personal percent-risk basis. Existing configs default to actual broker EOD cash.
    # virtual_eod lets a deliberately smaller funded broker balance track a separate
    # reference account while preserving the same cumulative realized P&L.
    personal_balance_mode: Literal["actual_eod", "virtual_eod"] = "actual_eod"
    personal_virtual_start_balance: float = Field(default=0.0, ge=0)
    personal_broker_anchor_balance: float = Field(default=0.0, ge=0)
    notes: str = ""

    @model_validator(mode="after")
    def required_prop_fields(self):
        if self.account_type in {"challenge", "funded"} and self.max_loss <= 0:
            raise ValueError("prop account requires max_loss")
        if self.account_type == "challenge" and self.challenge_target <= 0:
            raise ValueError("challenge requires challenge_target")
        if self.account_type == "personal" and self.personal_risk_method == "percent":
            if self.personal_risk_value <= 0:
                raise ValueError("personal percent risk requires personal_risk_value > 0")
            if self.personal_balance_mode == "virtual_eod":
                if self.personal_virtual_start_balance <= 0:
                    raise ValueError("virtual_eod requires personal_virtual_start_balance > 0")
                if self.personal_broker_anchor_balance <= 0:
                    raise ValueError("virtual_eod requires personal_broker_anchor_balance > 0")
        return self


class AccountState(BaseModel):
    account_id: str
    closed_cash_balance: float
    mll_floor: Optional[float] = None
    mll_verified: bool = False
    funded_locked: bool = False
    realized_today: float = 0.0
    largest_winning_day: float = 0.0
    state_timestamp: datetime
    daily_ledger_verified: bool = False
    source: str = "broker"


class VerifiedRiskState(BaseModel):
    """Separately verified prop-state facts that cash balance alone cannot prove."""
    account_id: str
    mll_floor: Optional[float] = None
    mll_verified: bool = False
    funded_locked: bool = False
    largest_winning_day: float = 0.0
    ledger_verified: bool = False
    cycle_start_utc: Optional[datetime] = None
    verified_at: datetime
    source: str = "manual_or_external_verified"


class CanonicalPlan(BaseModel):
    event_id: str
    engine: Literal["ORG", "SILVER", "CORE", "TGIF", "DWC", "ASW"]
    side: Side
    entry: float
    stop: float
    tp1: float
    tp2: Optional[float] = None
    module: str = ""
    source: str = ""
    score: int = 0
    reentry: bool = False
    reentry_type: str = ""
    native_time_ms: Optional[int] = None
    native_bar_index: Optional[int] = None
    source_qty: Optional[int] = None
    contract_risk_dollars: Optional[float] = None
    expiry_time_ms: Optional[int] = None
    contract_version: str = ""
    # Preserve Pine's frozen ORG sizing decision through durable plan JSON.
    # Shadow mode records malformed metadata; live allocators validate it before PLACE.
    org_regime_fields: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_geometry(self):
        if self.engine == "CORE" and self.tp2 is None:
            raise ValueError("Core requires tp2")
        if self.side == "LONG" and not self.stop < self.entry:
            raise ValueError("long stop must be below entry")
        if self.side == "SHORT" and not self.stop > self.entry:
            raise ValueError("short stop must be above entry")
        if self.engine == "ASW":
            if self.contract_version != "ASW_LIMIT_V1":
                raise ValueError("ASW requires ASW_LIMIT_V1 contract")
            if self.source_qty is None or self.source_qty < 1:
                raise ValueError("ASW requires native source_qty >= 1")
            if self.native_time_ms is None or self.expiry_time_ms is None:
                raise ValueError("ASW requires signal and expiry timestamps")
            if self.expiry_time_ms <= self.native_time_ms:
                raise ValueError("ASW expiry must be after signal time")
            if self.contract_risk_dollars is None or self.contract_risk_dollars <= 0:
                raise ValueError("ASW requires contract risk")
            if self.side == "LONG" and not self.entry < self.tp1:
                raise ValueError("ASW long target must be above entry")
            if self.side == "SHORT" and not self.entry > self.tp1:
                raise ValueError("ASW short target must be below entry")
        return self


class Allocation(BaseModel):
    account_id: str
    event_id: str
    engine: str
    side: Side
    qty: int
    entry: float
    stop: float
    tp1: float
    tp2: Optional[float] = None
    tp1_qty: int
    runner_qty: int
    base_risk: float
    effective_risk_budget: float
    risk_per_contract: float
    skip_reason: str = ""
    state_fallback: bool = False
    state_fallback_reason: str = ""


class AswPending(BaseModel):
    account_id: str
    crosstrade_account: str
    event_id: str
    side: Side
    qty: int
    native_qty: int
    entry: float
    stop: float
    target: float
    risk_per_contract: float
    signal_time_ms: int
    expiry_time_ms: int
    parent_order_id: str
    custom_order_id: str
    child_order_ids: list[str] = []
    created_at: datetime


class MarketPulse(BaseModel):
    native_time_ms: int
    native_close_time_ms: int
    native_bar_index: int
    open: float
    high: float
    low: float
    close: float
    atr: float
    runner_trail_low: Optional[float] = None
    runner_trail_high: Optional[float] = None


class ActiveTrade(BaseModel):
    account_id: str
    event_id: str
    engine: str
    side: Side
    entry: float
    initial_stop: float
    current_stop: float
    tp1: float
    tp2: Optional[float] = None
    total_qty: int
    tp1_qty: int
    runner_qty: int
    current_position_qty: int
    previous_position_qty: int
    entry_native_bar_index: Optional[int] = None
    stop_stage: int = 0
    tp1_filled: bool = False
    stop_order_ids: list[str] = []
    target_order_ids: list[str] = []
    parent_order_id: Optional[str] = None
    custom_order_id: Optional[str] = None
    org_attempt_key: Optional[str] = None


EntryAttemptState = Literal[
    "PREPARED", "SUBMITTING", "ACCEPTED", "ACTIVE", "CLOSED",
    "FLATTENING", "FLAT", "FAILED", "ABORTED",
]


class EntryAttempt(BaseModel):
    """Durable per-account mutation state around the accepted-order boundary.

    The row exists before PLACE and survives until the accepted broker exposure is
    promoted to ActiveTrade or reaches a terminal state. This closes the crash/race
    window where an EXIT or restart previously could not see a live order.
    """
    attempt_key: str
    account_id: str
    crosstrade_account: str
    event_id: str
    engine: str
    side: Side
    qty: int
    planned_entry: float
    stop: float
    tp1: float
    tp2: Optional[float] = None
    tp1_qty: int
    runner_qty: int
    custom_order_id: str
    entry_native_bar_index: Optional[int] = None
    entry_receipt_epoch: float = 0.0
    inbox_event_key: str = ""
    state: EntryAttemptState = "PREPARED"
    parent_order_id: Optional[str] = None
    target_order_ids: list[str] = []
    stop_order_ids: list[str] = []
    child_order_ids: list[str] = []
    preexisting_order_ids: list[str] = []
    actual_entry: Optional[float] = None
    accepted_at_epoch: Optional[float] = None
    submit_started_at_epoch: Optional[float] = None
    created_at_epoch: float
    updated_at_epoch: float
    reconcile_attempts: int = 0
    next_reconcile_at_epoch: float = 0.0
    last_error: str = ""
