import pytest

from onboarding import AutoOnboardingRejected, _closed_cash


def test_current_crosstrade_total_cash_value_is_closed_cash():
    detail = {
        "accountId": "68691898",
        "name": "FNFTCHJOSHUALYON75290",
        "balance": {
            "totalCashValue": 50_000,
            "netLiq": 50_000,
            "cashUSD": 50_000,
        },
    }
    assert _closed_cash(detail) == 50_000.0


def test_cash_usd_is_supported_when_total_cash_value_is_absent():
    assert _closed_cash({"balance": {"cashUSD": 50_000}}) == 50_000.0


def test_total_cash_value_precedes_cash_usd_fallback():
    detail = {"balance": {"totalCashValue": 49_975, "cashUSD": 50_000}}
    assert _closed_cash(detail) == 49_975.0


def test_net_liq_alone_remains_rejected():
    with pytest.raises(AutoOnboardingRejected, match="closed-cash balance is missing"):
        _closed_cash({"balance": {"netLiq": 50_125}})
