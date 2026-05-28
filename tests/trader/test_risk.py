"""Tests for the rule engine — the safety surface. Every rejection path the
validator can take has a test here, because a missed invariant is a hole the
model can drive a bad trade through."""
from datetime import date, timedelta

import pytest

from backend.trader.config import RiskConfig
from backend.trader.plan import TradePlan
from backend.trader.risk import RiskState, catalog_refs, validate_plan

RISK = RiskConfig(
    max_position_pct=0.25,
    max_risk_pct=0.01,
    min_rr=1.5,
    max_open_positions=3,
    max_symbol_exposure_pct=0.25,
    daily_loss_cap_pct=0.03,
    equity_floor_pct=0.80,
)

FEATURES = {
    "fvgs": [{"type": "fvg_bullish"}, {"type": "fvg_bearish"}],
    "order_blocks": [{"type": "ob_bullish"}],
    "structure": [{"type": "bos_bullish"}],
    "liquidity_sweeps": [],
}


def _validate(plan, **over):
    kwargs = dict(equity=10_000.0, open_symbols=set(), symbol="BTCUSDT", risk=RISK)
    kwargs.update(over)
    return validate_plan(plan, FEATURES, **kwargs)


# --- catalog ------------------------------------------------------------
def test_catalog_refs_enumerates_every_event():
    refs = set(catalog_refs(FEATURES))
    assert refs == {"fvgs:0", "fvgs:1", "order_blocks:0", "structure:0"}
    assert "liquidity_sweeps:0" not in refs  # empty group contributes nothing


# --- safe actions -------------------------------------------------------
def test_wait_always_accepted():
    assert _validate(TradePlan(action="wait", rationale="nothing here")).accepted


def test_close_existing_always_accepted():
    assert _validate(TradePlan(action="close_existing", rationale="bail")).accepted


# --- the phantom-setup defence -----------------------------------------
def test_open_without_detector_refs_rejected():
    plan = TradePlan(action="open_long", rationale="vibes", entry=100, stop=98, take_profit=105)
    res = _validate(plan)
    assert not res.accepted
    assert any("detector_refs" in r for r in res.reasons)


def test_phantom_detector_ref_rejected():
    plan = TradePlan(
        action="open_long", rationale="made it up", entry=100, stop=98, take_profit=105,
        detector_refs=["order_blocks:99"],
    )
    res = _validate(plan)
    assert not res.accepted
    assert any("phantom" in r for r in res.reasons)


# --- geometry + R:R -----------------------------------------------------
def test_missing_prices_rejected():
    plan = TradePlan(action="open_long", rationale="x", detector_refs=["order_blocks:0"])
    res = _validate(plan)
    assert not res.accepted
    assert any("entry" in r for r in res.reasons)


def test_inverted_long_geometry_rejected():
    # stop above entry — the classic inverted setup
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=102, take_profit=110,
        detector_refs=["order_blocks:0"],
    )
    assert not _validate(plan).accepted


def test_reward_risk_below_floor_rejected():
    # risk 2, reward 1 -> rr 0.5 < 1.5
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=98, take_profit=101,
        detector_refs=["order_blocks:0"],
    )
    res = _validate(plan)
    assert not res.accepted
    assert any("reward:risk" in r for r in res.reasons)


def test_short_geometry_valid():
    plan = TradePlan(
        action="open_short", rationale="x", entry=100, stop=110, take_profit=70,
        detector_refs=["order_blocks:0"],
    )
    res = _validate(plan)
    assert res.accepted
    assert res.side == "short"


# --- concurrency --------------------------------------------------------
def test_already_in_symbol_rejected():
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=98, take_profit=110,
        detector_refs=["order_blocks:0"],
    )
    res = _validate(plan, open_symbols={"BTCUSDT"})
    assert not res.accepted
    assert any("already holding" in r for r in res.reasons)


def test_concurrency_cap_rejected():
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=98, take_profit=110,
        detector_refs=["order_blocks:0"],
    )
    res = _validate(plan, open_symbols={"A", "B", "C"})
    assert not res.accepted
    assert any("concurrency" in r for r in res.reasons)


# --- sizing -------------------------------------------------------------
def test_sizing_clamped_by_risk_cap():
    # risk_dist 20, equity 10k, max_risk 1% -> max risk cash 100 -> qty 5
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=80, take_profit=160,
        size_pct_equity=1.0, detector_refs=["order_blocks:0"],
    )
    res = _validate(plan)
    assert res.accepted
    assert res.qty == pytest.approx(5.0)
    assert res.risk_cash == pytest.approx(100.0)  # exactly the 1% cap


def test_sizing_clamped_by_notional_cap():
    # tiny risk_dist -> risk cap is loose, notional cap binds
    # notional cap = min(0.25, 0.25)*10k = 2500 -> qty 25 at entry 100
    plan = TradePlan(
        action="open_long", rationale="x", entry=100, stop=99.9, take_profit=100.3,
        size_pct_equity=1.0, detector_refs=["order_blocks:0"],
    )
    res = _validate(plan)
    assert res.accepted
    assert res.qty == pytest.approx(25.0)
    assert res.notional == pytest.approx(2500.0)


def test_oversize_request_is_clamped_not_rejected():
    # model asks for 100% of equity; it must come back bounded, still accepted
    plan = TradePlan(
        action="open_long", rationale="greedy", entry=100, stop=80, take_profit=160,
        size_pct_equity=1.0, detector_refs=["order_blocks:0"],
    )
    res = _validate(plan)
    assert res.accepted
    assert res.notional <= 0.25 * 10_000 + 1e-6
    assert res.risk_cash <= 0.01 * 10_000 + 1e-6


# --- RiskState halts ----------------------------------------------------
def test_daily_loss_cap_halts_entries():
    rs = RiskState(risk=RISK)
    rs.on_equity(10_000.0, 10_000.0, date(2026, 1, 1))
    assert rs.entries_allowed()
    rs.record_realized(-250.0)
    assert rs.entries_allowed()       # -2.5%, under the 3% cap
    rs.record_realized(-100.0)        # -3.5% total, over the cap
    assert not rs.entries_allowed()
    assert rs.halted_today


def test_new_day_clears_daily_halt():
    rs = RiskState(risk=RISK)
    rs.on_equity(10_000.0, 10_000.0, date(2026, 1, 1))
    rs.record_realized(-400.0)
    assert not rs.entries_allowed()
    rs.on_equity(9_600.0, 10_000.0, date(2026, 1, 2))
    assert rs.entries_allowed()
    assert rs.realized_today == 0.0


def test_equity_floor_is_permanent():
    rs = RiskState(risk=RISK)
    rs.on_equity(7_999.0, 10_000.0, date(2026, 1, 1))  # below 80% of HWM
    assert rs.permanent_halt
    assert not rs.entries_allowed()
    # a new day must NOT clear a permanent halt
    rs.on_equity(7_999.0, 10_000.0, date(2026, 1, 2))
    assert rs.permanent_halt
    assert not rs.entries_allowed()


def test_kill_then_rearm():
    rs = RiskState(risk=RISK)
    rs.on_equity(10_000.0, 10_000.0, date(2026, 1, 1))
    rs.kill()
    assert rs.killed and not rs.entries_allowed()
    rs.rearm()
    assert rs.entries_allowed()
