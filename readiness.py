from __future__ import annotations
from pathlib import Path
from models import AccountRule
from settings import Settings
from crosstrade import InstrumentContractError, normalize_tradovate_symbol


def readiness(settings: Settings, accounts: list[AccountRule]) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    execution_mode = str(settings.AUTOPROP_EXECUTION_MODE).strip().lower()
    if execution_mode not in {'live', 'shadow'}:
        # Preserve the prior diagnostic for operators/tests while stating the expanded
        # contract introduced by the shadow-observer release.
        problems.append('execution mode must be live')
        problems.append('supported execution modes are live or shadow')
    if settings.AUTOPROP_MANAGEMENT_MODE != 'exact_formula_parity':
        problems.append('management mode must be exact_formula_parity')
    if settings.NATIVE_ATM_BREAKEVEN_AFTER_TP1:
        problems.append('native ATM breakeven must be disabled')
    if settings.LIVE_MAX_QTY_PER_ACCOUNT != 0:
        problems.append('LIVE_MAX_QTY_PER_ACCOUNT must be 0 for formula parity')
    try:
        normalize_tradovate_symbol(settings.DEFAULT_EXECUTION_SYMBOL)
    except InstrumentContractError as exc:
        problems.append(f'execution symbol invalid: {exc}')
    if execution_mode == 'live':
        if settings.AUTOPROP_LIVE_ARM != 'I_UNDERSTAND_LIVE_ORDERS':
            problems.append('live arm missing')
        if settings.AUTOPROP_FULL_SCALE_ARM != 'I_UNDERSTAND_FULL_SCALE':
            problems.append('full-scale arm missing')
        if not settings.CROSSTRADE_TOKEN:
            problems.append('CrossTrade token missing')
    elif execution_mode == 'shadow':
        warnings.append('shadow mode active: broker mutations are disabled')
        if Path(settings.SHADOW_SQLITE_PATH).resolve() == Path(settings.SQLITE_PATH).resolve():
            problems.append('SHADOW_SQLITE_PATH must differ from SQLITE_PATH')
        if settings.SHADOW_BROKER_READS_ENABLED:
            problems.append('shadow broker reads are not implemented in this release; set SHADOW_BROKER_READS_ENABLED=false')
            if not settings.CROSSTRADE_TOKEN:
                problems.append('CrossTrade token missing for shadow broker reads')
    if not settings.AUTOPROP_WEBHOOK_TOKEN:
        problems.append('webhook token missing')
    enabled = [a for a in accounts if a.enabled]
    if not accounts:
        problems.append('account registry empty')
    if not enabled:
        problems.append('no enabled accounts')
    for a in enabled:
        if not a.rules_verified:
            problems.append(f'{a.account_id}: enabled but rules not verified')
    if not settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
        warnings.append('TradingView alert contract gate is false (expected while staging)')
    configuration_ready = not problems
    return {
        'execution_mode': execution_mode,
        'shadow_mode': execution_mode == 'shadow',
        'configuration_ready': configuration_ready,
        'broker_mutation_armed': (
            execution_mode == 'live'
            and configuration_ready
            and settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED
        ),
        'problems': problems,
        'warnings': warnings,
        'registered_accounts': len(accounts),
        'enabled_accounts': len(enabled),
    }
