from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', case_sensitive=False)
    CROSSTRADE_BASE_URL: str = 'https://app.crosstrade.io'
    CROSSTRADE_TOKEN: str = ''
    AUTOPROP_WEBHOOK_TOKEN: str = ''
    SQLITE_PATH: str = '/data/autoprop_router_v123.sqlite3'
    ACCOUNT_CONFIG_PATH: str = '/data/accounts.json'
    ACCOUNT_CONFIG_JSON: str = ''
    RISK_STATE_JSON: str = ''
    AUTO_DISCOVERY: bool = True
    FUNDEDNEXT_DEFAULT_MODEL: str = 'Legacy'
    STATE_POLL_SECONDS: int = 10
    MAX_STATE_AGE_SECONDS: int = 20
    REQUEST_TIMEOUT_SECONDS: float = 6.0
    CROSSTRADE_RATE_LIMIT_PER_MINUTE: int = 150
    CROSSTRADE_RATE_LIMIT_WINDOW_SECONDS: float = 60.0
    CROSSTRADE_RATE_LIMIT_MAX_RETRIES: int = 8
    CROSSTRADE_RATE_LIMIT_FALLBACK_SECONDS: float = 1.0
    CROSSTRADE_REQUEST_MIN_INTERVAL_SECONDS: float = 0.10
    # Broker reads stay paced, while one coordinated eight-account entry may use the
    # documented burst allowance. Mutations still pass through the rolling-minute cap.
    CROSSTRADE_MUTATION_MIN_INTERVAL_SECONDS: float = 0.02
    CROSSTRADE_SAFE_GET_MAX_CONCURRENCY: int = 2
    CROSSTRADE_GET_RETRY_MAX_RETRIES: int = 3
    CROSSTRADE_GET_RETRY_DELAY_SECONDS: float = 0.75
    CROSSTRADE_GET_RETRY_BACKOFF_MULTIPLIER: float = 2.0
    CROSSTRADE_GET_RETRY_MAX_DELAY_SECONDS: float = 5.0
    DEFAULT_EXECUTION_SYMBOL: str = 'MNQ1!'
    MNQ_POINT_VALUE: float = 2.0
    MNQ_TICK_SIZE: float = 0.25
    MODELED_ROUND_TRIP_COST: float = 2.90
    CONSISTENCY_MIN_PROFIT_ROOM: float = 50.0
    USE_CROSSTRADE_POSITION_GATE: bool = True
    USE_CROSSTRADE_MAX_POSITIONS_GATE: bool = False
    AUTOPROP_EXECUTION_MODE: str = 'live'
    AUTOPROP_MANAGEMENT_MODE: str = 'exact_formula_parity'
    LIVE_MAX_QTY_PER_ACCOUNT: int = 0
    NATIVE_ATM_BREAKEVEN_AFTER_TP1: bool = False
    EXECUTION_REQUIRE_PROTECTIVE_STOP: bool = True
    MANAGEMENT_CHANGE_RETRIES: int = 3
    MANAGEMENT_CHANGE_RETRY_DELAY_SECONDS: float = 0.25
    BRACKET_CONFIRM_RETRIES: int = 12
    BRACKET_CONFIRM_RETRY_DELAY_SECONDS: float = 0.25
    # An ENTRY is admitted only while it is fresh from Router ingress. Account
    # preflight is performed as a batch, followed by a tight broker mutation wave.
    ENTRY_SIGNAL_MAX_AGE_SECONDS: float = 8.0
    ENTRY_PREFLIGHT_TIMEOUT_SECONDS: float = 5.0
    ENTRY_FANOUT_MAX_CONCURRENCY: int = 8
    # Core ATM child discovery/normalization runs after the low-latency PLACE wave.
    # Eight accounts can consume most of one broker read window, so allow one full
    # guarded reconciliation cycle before fail-safe flattening.
    ENTRY_RECONCILE_TIMEOUT_SECONDS: float = 90.0
    CORE_NORMALIZATION_MAX_CONCURRENCY: int = 8
    ENTRY_RECONCILE_BASE_DELAY_SECONDS: float = 0.75
    ENTRY_RECONCILE_MAX_DELAY_SECONDS: float = 5.0
    ENTRY_RECONCILE_LOOP_SECONDS: float = 0.50
    ENTRY_STATE_CACHE_MAX_AGE_SECONDS: float = 20.0
    STARTUP_STATE_WARM_TIMEOUT_SECONDS: float = 20.0
    AUTOPROP_LIVE_ARM: str = ''
    AUTOPROP_FULL_SCALE_ARM: str = ''
    TRADINGVIEW_ALERT_CONTRACT_VERIFIED: bool = False
    DEDUPE_RETENTION_DAYS: int = 30
    ASW_PENDING_RECONCILE_SECONDS: float = 10.0
    ASW_PROTECTION_CONFIRM_RETRIES: int = 4
    ASW_PROTECTION_CONFIRM_RETRY_DELAY_SECONDS: float = 0.25
