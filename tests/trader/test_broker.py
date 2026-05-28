"""Paper broker fills, fees, slippage, and exit resolution."""
from datetime import datetime, timezone

import pytest

from backend.trader.broker import PaperBroker

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _broker(slip=10.0, fee=10.0):
    # 10 bps slippage, 10 bps fee
    return PaperBroker(starting_equity=10_000.0, slippage_bps=slip, fee_bps=fee)


def test_entry_applies_adverse_slippage_and_fee():
    b = _broker()
    pos = b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=130.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    assert pos.entry_price == pytest.approx(100.1)        # long fills 0.1% higher
    assert b.realized_equity == pytest.approx(10_000.0 - 0.1001)  # entry fee charged


def test_long_take_profit_exit():
    b = _broker()
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=120.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    trade = b.check_exits("BTCUSDT", bar_high=125.0, bar_low=99.0, exit_time=T0)
    assert trade is not None
    assert trade.reason == "take_profit"
    assert trade.exit_price == pytest.approx(120.0)       # limit, no slippage
    # gross = (120 - 100.1)*1 = 19.9 ; exit fee = 120*0.001 = 0.12
    assert trade.gross_pnl == pytest.approx(19.9)
    assert trade.net_pnl == pytest.approx(19.78)
    assert "BTCUSDT" not in b.positions


def test_long_stop_exit_has_adverse_slippage():
    b = _broker()
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=130.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    trade = b.check_exits("BTCUSDT", bar_high=101.0, bar_low=85.0, exit_time=T0)
    assert trade is not None
    assert trade.reason == "stop_loss"
    assert trade.exit_price == pytest.approx(90.0 * 0.999)  # stop slips through
    assert trade.net_pnl < 0


def test_stop_checked_before_target_when_bar_straddles_both():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=95.0, take_profit=105.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    # one bar that hits BOTH 95 and 105 -> pessimism says stop
    trade = b.check_exits("BTCUSDT", bar_high=106.0, bar_low=94.0, exit_time=T0)
    assert trade.reason == "stop_loss"


def test_short_stop_exit():
    b = _broker()
    b.open_position(
        symbol="ETHUSDT", side="short", qty=1.0, requested_price=100.0,
        stop=110.0, take_profit=80.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    assert b.positions["ETHUSDT"].entry_price == pytest.approx(99.9)  # short fills lower
    trade = b.check_exits("ETHUSDT", bar_high=111.0, bar_low=100.0, exit_time=T0)
    assert trade.reason == "stop_loss"
    assert trade.exit_price == pytest.approx(110.0 * 1.001)  # buy-to-cover slips up


def test_equity_tracks_unrealized_and_high_water_mark():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=2.0, requested_price=100.0,
        stop=90.0, take_profit=130.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    eq = b.equity({"BTCUSDT": 110.0})
    assert eq == pytest.approx(10_000.0 + 20.0)            # +10 * 2 units
    assert b.high_water_mark == pytest.approx(10_020.0)
    # mark back down: equity falls but HWM sticks
    eq2 = b.equity({"BTCUSDT": 95.0})
    assert eq2 == pytest.approx(10_000.0 - 10.0)
    assert b.high_water_mark == pytest.approx(10_020.0)


_MGMT = {"breakeven_at_r": 1.0, "trail_at_r": 2.0, "trail_r": 1.0}


def test_no_stop_move_before_breakeven_threshold():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=130.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    # +0.5R only -> nothing moves
    assert b.manage_stops("BTCUSDT", bar_high=105.0, bar_low=99.0, **_MGMT) is None
    assert b.positions["BTCUSDT"].stop == pytest.approx(90.0)


def test_breakeven_move_at_one_r_covers_costs():
    b = _broker(slip=10.0, fee=10.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=200.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    entry = b.positions["BTCUSDT"].entry_price            # 100.1, R = 10.1
    moved = b.manage_stops("BTCUSDT", bar_high=111.0, bar_low=100.0, **_MGMT)  # ~+1.08R
    assert moved is not None and moved["kind"] == "breakeven"
    # stop sits just ABOVE entry, so a stop-out here is a scratch, not a loss
    assert b.positions["BTCUSDT"].stop > entry


def test_trailing_locks_in_profit_and_only_tightens():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=500.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )  # entry 100, R = 10
    moved = b.manage_stops("BTCUSDT", bar_high=130.0, bar_low=100.0, **_MGMT)  # +3R
    assert moved["kind"] == "trail"
    assert b.positions["BTCUSDT"].stop == pytest.approx(120.0)  # peak 130 - 1R(10)
    # a weaker bar must never loosen the stop
    assert b.manage_stops("BTCUSDT", bar_high=122.0, bar_low=118.0, **_MGMT) is None
    assert b.positions["BTCUSDT"].stop == pytest.approx(120.0)


def test_short_breakeven_moves_stop_down():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="ETHUSDT", side="short", qty=1.0, requested_price=100.0,
        stop=110.0, take_profit=40.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )  # entry 100, R = 10
    moved = b.manage_stops("ETHUSDT", bar_high=100.0, bar_low=90.0, **_MGMT)  # +1R
    assert moved["kind"] == "breakeven"
    assert b.positions["ETHUSDT"].stop == pytest.approx(100.0)  # zero costs -> exactly entry
    assert b.positions["ETHUSDT"].stop < 110.0                  # tightened from initial


def test_close_at_market_settles_realized():
    b = _broker(slip=0.0, fee=0.0)
    b.open_position(
        symbol="BTCUSDT", side="long", qty=1.0, requested_price=100.0,
        stop=90.0, take_profit=130.0, entry_time=T0, validity_bars=10, plan_id="p1",
    )
    trade = b.close_at_market("BTCUSDT", 108.0, "manual", T0)
    assert trade.net_pnl == pytest.approx(8.0)
    assert b.realized_equity == pytest.approx(10_008.0)
    assert b.close_at_market("BTCUSDT", 108.0, "manual", T0) is None  # nothing left
