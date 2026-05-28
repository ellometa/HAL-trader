"""TradePlan schema sanity. The validator does the heavy lifting; this just
pins the shape and the safe defaults."""
import pytest
from pydantic import ValidationError

from backend.trader.plan import TradePlan


def test_minimal_wait_plan():
    p = TradePlan(action="wait", rationale="nothing actionable")
    assert p.action == "wait"
    assert p.entry is None
    assert p.confidence == "low"          # conservative default
    assert p.detector_refs == []
    assert p.validity_bars == 12


def test_full_open_plan_roundtrips_json():
    p = TradePlan(
        action="open_long", rationale="ob + bos", entry=100, stop=98,
        take_profit=110, size_pct_equity=0.2, confidence="high",
        detector_refs=["order_blocks:0", "structure:1"],
    )
    again = TradePlan.model_validate_json(p.model_dump_json())
    assert again == p


def test_bad_action_rejected():
    with pytest.raises(ValidationError):
        TradePlan(action="moon", rationale="x")  # type: ignore[arg-type]


def test_validity_bars_must_be_positive():
    with pytest.raises(ValidationError):
        TradePlan(action="wait", rationale="x", validity_bars=0)
