"""Confluence scoring tests. Hand-built feature dicts (same shape the real
detectors emit) so we can assert exactly which factors fire and that an A+
stack outscores a bare zone — the property the rest of the policy leans on.
"""
from backend.trader import confluence
from backend.trader.confluence import best_setup, score_for_refs


def _features(*, order_blocks=None, fvgs=None, structure=None, sweeps=None):
    return {
        "order_blocks": order_blocks or [],
        "fvgs": fvgs or [],
        "structure": structure or [],
        "liquidity_sweeps": sweeps or [],
    }


def _bull_ob(price_low=90.0, price_high=95.0, end_index=18, mitigated=False):
    return {
        "type": "ob_bullish",
        "start_index": end_index - 1,
        "end_index": end_index,
        "price_low": price_low,
        "price_high": price_high,
        "mitigated": mitigated,
        "meta": {"bos_index": end_index},
    }


def test_bare_order_block_scores_low_but_valid():
    # An unmitigated bullish OB below price, nothing else aligned.
    feats = _features(order_blocks=[_bull_ob()])
    s = best_setup(feats, current_price=100.0, n_bars=20)
    assert s is not None
    assert s.side == "long"
    assert s.entry == 100.0
    assert s.stop == 90.0          # OB low
    assert s.points == confluence.W_ZONE + confluence.W_FRESH  # zone + recent only
    assert s.anchor_ref == "order_blocks:0"
    assert "order_blocks:0" in s.detector_refs


def test_full_stack_is_an_a_plus_long():
    feats = _features(
        order_blocks=[_bull_ob()],
        structure=[{"type": "choch_bullish", "swing_index": 10, "break_index": 15, "price": 99.0}],
        fvgs=[{
            "type": "fvg_bullish", "start_index": 16, "end_index": 18,
            "price_low": 92.0, "price_high": 96.0, "mitigated": False, "meta": {},
        }],
        sweeps=[{
            "type": "sweep_bullish", "swing_index": 12, "sweep_index": 17,
            "level": 89.0, "wick": 88.0, "close": 91.0, "meta": {},
        }],
    )
    s = best_setup(feats, current_price=100.0, n_bars=20, min_score=0.5)
    assert s is not None and s.side == "long"
    # zone + structure + choch + fvg + sweep + fresh == all 10 points
    assert s.points == confluence._MAX_POINTS
    assert s.score == 1.0
    # every contributing detector is cited, in catalog format
    assert {"order_blocks:0", "structure:0", "fvgs:0", "liquidity_sweeps:0"} <= set(s.detector_refs)
    assert any("CHoCH" in f for f in s.factors)


def test_min_score_filters_weak_setups():
    feats = _features(order_blocks=[_bull_ob()])  # ~0.3 score
    assert best_setup(feats, 100.0, n_bars=20, min_score=0.6) is None
    assert best_setup(feats, 100.0, n_bars=20, min_score=0.0) is not None


def test_mitigated_zone_is_ignored():
    feats = _features(order_blocks=[_bull_ob(mitigated=True)])
    assert best_setup(feats, 100.0, n_bars=20) is None


def test_higher_confluence_direction_wins():
    # A bare short zone vs a structure-backed long zone -> long should win.
    feats = _features(
        order_blocks=[
            _bull_ob(price_low=90.0, price_high=95.0),
            {"type": "ob_bearish", "start_index": 17, "end_index": 18,
             "price_low": 105.0, "price_high": 110.0, "mitigated": False, "meta": {}},
        ],
        structure=[{"type": "bos_bullish", "swing_index": 10, "break_index": 15, "price": 99.0}],
    )
    s = best_setup(feats, current_price=100.0, n_bars=20)
    assert s is not None and s.side == "long"


def test_stale_context_does_not_count_as_fresh():
    # OB and sweep both far in the past relative to n_bars -> no FRESH/SWEEP pts.
    feats = _features(
        order_blocks=[_bull_ob(end_index=2)],
        sweeps=[{"type": "sweep_bullish", "swing_index": 1, "sweep_index": 2,
                 "level": 89.0, "wick": 88.0, "close": 91.0, "meta": {}}],
    )
    s = best_setup(feats, current_price=100.0, n_bars=100, recent_window=20)
    assert s is not None
    assert s.points == confluence.W_ZONE   # neither fresh nor recent-sweep
    assert all("sweep" not in f for f in s.factors)


def test_no_chase_guard_rejects_far_entries():
    # OB [90, 95], zone height 5. With mult=1.0 the entry must be <= 100.
    feats = _features(order_blocks=[_bull_ob(price_low=90.0, price_high=95.0)])
    assert best_setup(feats, 100.0, n_bars=20, max_entry_zone_mult=1.0) is not None
    # price ran to 130 -> chasing -> rejected even though the zone is real
    assert best_setup(feats, 130.0, n_bars=20, max_entry_zone_mult=1.0) is None
    # guard disabled -> the far entry is allowed again
    assert best_setup(feats, 130.0, n_bars=20, max_entry_zone_mult=None) is not None


def test_score_for_refs_ignores_entry_proximity():
    # The validator gate scores the setup regardless of how far price has run.
    feats = _features(
        order_blocks=[_bull_ob(price_low=90.0, price_high=95.0)],
        structure=[{"type": "bos_bullish", "swing_index": 10, "break_index": 15, "price": 99.0}],
    )
    assert score_for_refs(feats, 500.0, "long", n_bars=20) > 0.0


def test_score_for_refs_matches_best_setup():
    feats = _features(
        order_blocks=[_bull_ob()],
        structure=[{"type": "bos_bullish", "swing_index": 10, "break_index": 15, "price": 99.0}],
    )
    assert score_for_refs(feats, 100.0, "long", n_bars=20) > 0.0
    assert score_for_refs(feats, 100.0, "short", n_bars=20) == 0.0
