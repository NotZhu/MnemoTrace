"""Budget and credential checks for historical extraction measurements."""
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL = Path(__file__).resolve().parents[1] / 'eval'


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(EVAL))
    return importlib.import_module('extraction_runner')


def test_budget_stops_on_missing_usage_without_inventing_zero_cost(harness, tmp_path):
    budget = harness.Budget.create(tmp_path/'budget.json', calls=2)
    budget.claim()
    budget.finish(None)
    with pytest.raises(harness.BudgetStopped):
        budget.claim()


def test_budget_counts_failed_requests_and_enforces_call_limit(harness, tmp_path):
    budget = harness.Budget.create(tmp_path/'budget.json', calls=1)
    budget.claim()
    budget.finish({'total_tokens': 5})
    with pytest.raises(harness.BudgetStopped):
        budget.claim()


def test_missing_deepseek_credentials_fail_before_model_or_store(harness, monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    args = SimpleNamespace(extractor='deepseek', out=tmp_path)
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        harness.run_case({}, args)
    assert not list(tmp_path.iterdir())


def test_current_runtime_is_rejected_before_extraction(harness, monkeypatch, tmp_path):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-key')
    args = SimpleNamespace(extractor='deepseek', out=tmp_path)
    with pytest.raises(ValueError, match='Select the historical runtime'):
        harness.run_case({}, args)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('limits', [{'calls': 0}, {'seconds': -1}, {'tokens': True}])
def test_invalid_budget_does_not_create_ledger(harness, tmp_path, limits):
    target = tmp_path/'budget.json'
    with pytest.raises(ValueError):
        harness.Budget.create(target, **limits)
    assert not target.exists()
