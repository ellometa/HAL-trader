"""The decision prompt is a pure function — assert it carries the context the
model needs (confluence read, cost-adjusted R:R, the confluence floor) without
ever calling a model. The rule policy is exercised elsewhere; here we only
guard what the LLM is actually told."""
from backend.trader.brain import build_decision_prompt
from backend.trader.config import RiskConfig

RISK = RiskConfig(
    max_position_pct=0.25,
    max_risk_pct=0.01,
    min_rr=1.5,
    max_open_positions=3,
    max_symbol_exposure_pct=0.25,
    daily_loss_cap_pct=0.03,
    equity_floor_pct=0.80,
    min_confluence=0.5,
)

PORTFOLIO = {"equity": 10_000.0, "open_position": None, "open_symbols": [], "risk": {}}


def _bull_ob(price_low=90.0, price_high=95.0, end_index=18):
    return {
        "type": "ob_bullish", "start_index": end_index - 1, "end_index": end_index,
        "price_low": price_low, "price_high": price_high, "mitigated": False,
        "meta": {"bos_index": end_index},
    }


def _feats(**kw):
    base = {"order_blocks": [], "fvgs": [], "structure": [], "liquidity_sweeps": []}
    base.update(kw)
    return base


def _build(features, *, cost_bps=25.0, n_bars=20):
    return build_decision_prompt(
        symbol="BTCUSDT", timeframe="1h", current_price=100.0, features=features,
        portfolio=PORTFOLIO, notes="", risk=RISK, cost_bps=cost_bps, n_bars=n_bars,
    )


def test_system_prompt_states_cost_adjusted_rr_and_floor():
    system, _ = _build(_feats())
    assert "NET OF COSTS" in system
    assert "25" in system                       # the cost_bps value is shown
    assert "confluence floor of" in system      # the gate is disclosed
    assert "0.50" in system                     # ...at the configured threshold
    assert "1.5" in system                       # min_rr


def test_full_stack_surfaces_the_scorers_pick():
    feats = _feats(
        order_blocks=[_bull_ob()],
        structure=[{"type": "choch_bullish", "swing_index": 10, "break_index": 15, "price": 99.0}],
        fvgs=[{"type": "fvg_bullish", "start_index": 16, "end_index": 18,
               "price_low": 92.0, "price_high": 96.0, "mitigated": False, "meta": {}}],
        liquidity_sweeps=[{"type": "sweep_bullish", "swing_index": 12, "sweep_index": 17,
                           "level": 89.0, "wick": 88.0, "close": 91.0, "meta": {}}],
    )
    _, user = _build(feats)
    assert "Deterministic confluence read" in user
    assert "LONG" in user                       # the scorer recommends a long
    assert "clears the 0.50 floor" in user
    assert "order_blocks:0" in user             # the exact ref it's built on


def test_empty_features_tells_model_to_wait():
    _, user = _build(_feats())
    assert "NO order-block zone" in user
    assert "prefer wait" in user


def test_weak_setup_warns_below_floor():
    # a bare OB scores 0.3 (zone + fresh) -> under the 0.5 floor
    _, user = _build(_feats(order_blocks=[_bull_ob()]))
    assert "No setup clears the 0.50 confluence floor" in user
