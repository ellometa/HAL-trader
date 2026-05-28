"""End-to-end engine test on the deterministic rule policy (client=None), so
no network and no LLM. Crafted candles drive a real open, then a real
stop-loss exit, through the actual detectors, broker and journal.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.trader.broker import PaperBroker
from backend.trader.config import load_config
from backend.trader.engine import Engine
from backend.trader.journal import Journal
from backend.trader.risk import RiskState

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# A bullish order block (down candle at idx 6) confirmed by a BOS at idx 7.
# (Same geometry as tests/test_order_blocks.py::test_bullish_order_block.)
_OB_ROWS = [
    (97, 100, 95, 99),     # 0
    (99, 104, 96, 102),    # 1
    (102, 110, 100, 108),  # 2  swing high (110)
    (108, 109, 103, 104),  # 3
    (104, 105, 100, 101),  # 4
    (101, 106, 100, 105),  # 5
    (105, 106, 99, 100),   # 6  DOWN  <- bullish OB [99, 106]
    (100, 115, 99, 114),   # 7  BOS: close 114 > 110
]


def _df(rows):
    return pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(hours=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


@pytest.fixture
def engine(tmp_path):
    cfg = load_config()
    broker = PaperBroker(
        starting_equity=cfg.starting_equity,
        slippage_bps=cfg.fills.slippage_bps,
        fee_bps=cfg.fills.fee_bps,
    )
    return Engine(
        config=cfg,
        broker=broker,
        risk_state=RiskState(risk=cfg.risk),
        journal=Journal(tmp_path / "journal.jsonl"),
        notes="",
        client=None,  # forces the deterministic rule policy
    )


def test_open_then_stop_out(engine, monkeypatch):
    holder = {"df": _df(_OB_ROWS + [(114, 116, 113, 115)])}  # +1 forming bar

    async def fake_fetch(symbol, timeframe, limit):
        return holder["df"].copy()

    monkeypatch.setattr("backend.trader.engine.fetch_ohlc", fake_fetch)

    # --- cycle 1: the rule should open a long off the unmitigated OB -----
    out = asyncio.run(engine.run_cycle("BTCUSDT"))
    assert out["action"] == "open_long"
    assert out["executed"] is True
    assert "BTCUSDT" in engine.broker.positions
    pos = engine.broker.positions["BTCUSDT"]
    assert pos.side == "long"
    assert pos.stop == pytest.approx(99.0)        # OB low
    assert len(engine.journal.read_all()) == 1

    # --- cycle 2: a new closed bar pierces the stop -> exit --------------
    # rows 0..7 OB scene, row 8 = prior forming bar (now closed, the entry
    # bar), row 9 = stop bar (low 80 < stop 99), row 10 = new forming bar.
    holder["df"] = _df(
        _OB_ROWS
        + [(114, 116, 113, 115), (100, 100, 80, 82), (82, 84, 81, 83)]
    )
    out2 = asyncio.run(engine.run_cycle("BTCUSDT"))
    assert "BTCUSDT" not in engine.broker.positions          # flat again
    assert out2["exits"], "expected a stop-loss exit"
    assert out2["exits"][0]["reason"] == "stop_loss"
    assert out2["exits"][0]["net_pnl"] < 0
    assert engine.broker.realized_equity < engine.config.starting_equity
    assert len(engine.journal.read_all()) == 2


def test_no_setup_waits(engine, monkeypatch):
    # Flat, featureless candles -> no OB, no structure -> rule waits.
    flat = _df([(100, 101, 99, 100)] * 12)

    async def fake_fetch(symbol, timeframe, limit):
        return flat.copy()

    monkeypatch.setattr("backend.trader.engine.fetch_ohlc", fake_fetch)
    out = asyncio.run(engine.run_cycle("BTCUSDT"))
    assert out["action"] == "wait"
    assert out["executed"] is False
    assert not engine.broker.positions
