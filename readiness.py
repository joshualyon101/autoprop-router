from __future__ import annotations
from models import AccountRule
from settings import Settings


def readiness(settings: Settings, accounts: list[AccountRule]) -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    if settings.AUTOPROP_MANAGEMENT_MODE != 'exact_formula_parity':
        problems.append('management mode must be exact_formula_parity')
    if settings.NATIVE_ATM_BREAKEVEN_AFTER_TP1:
        problems.append('native ATM breakeven must be disabled')
    if settings.LIVE_MAX_QTY_PER_ACCOUNT != 0:
        problems.append('LIVE_MAX_QTY_PER_ACCOUNT must be 0 for formula parity')
    if settings.AUTOPROP_LIVE_ARM != 'I_UNDERSTAND_LIVE_ORDERS':
        problems.append('live arm missing')
    if settings.AUTOPROP_FULL_SCALE_ARM != 'I_UNDERSTAND_FULL_SCALE':
        problems.append('full-scale arm missing')
    if not settings.CROSSTRADE_TOKEN:
        problems.append('CrossTrade token missing')
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
        'configuration_ready': configuration_ready,
        'broker_mutation_armed': configuration_ready and settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED,
        'problems': problems,
        'warnings': warnings,
        'registered_accounts': len(accounts),
        'enabled_accounts': len(enabled),
    }
