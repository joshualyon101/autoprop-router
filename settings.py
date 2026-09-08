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
    REQUEST_TIMEOUT_SECONDS: float = 4.0
    CROSSTRADE_RATE_LIMIT_PER_MINUTE: int = 150
    CROSSTRADE_RATE_LIMIT_WINDOW_SECONDS: float = 60.0
    CROSSTRADE_RATE_LIMIT_MAX_RETRIES: int = 8
    CROSSTRADE_RATE_LIMIT_FALLBACK_SECONDS: float = 1.0
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
    AUTOPROP_LIVE_ARM: str = ''
    AUTOPROP_FULL_SCALE_ARM: str = ''
    TRADINGVIEW_ALERT_CONTRACT_VERIFIED: bool = False
    DEDUPE_RETENTION_DAYS: int = 30
