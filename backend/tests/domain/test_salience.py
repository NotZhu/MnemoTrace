"""验证显著度的基准、衰减和强化。"""

import pytest

from mnemotrace_next.domain.errors import DomainError
from mnemotrace_next.domain.salience import FactSalience

_HALF_LIFE = 35 * 86_400.0


def test_default_salience_holds_at_baseline() -> None:
    state = FactSalience()

    assert state.salience == 1.0
    assert state.last_access is None
    assert state.effective_at(_HALF_LIFE) == 1.0
    assert state.normalized_at(_HALF_LIFE) == 0.0


def test_reinforce_adds_to_the_decayed_boost() -> None:
    first = FactSalience().reinforce(0.0)
    assert first == FactSalience(1.5, 0.0)
    assert first.effective_at(_HALF_LIFE) == 1.25
    assert first.effective_at(2 * _HALF_LIFE) == 1.125

    second = first.reinforce(_HALF_LIFE)
    assert second == FactSalience(1.75, _HALF_LIFE)
    assert first == FactSalience(1.5, 0.0)


def test_reinforce_respects_the_cap_and_monotonic_access_time() -> None:
    state = FactSalience(2.75, 10.0)

    assert state.effective_at(5.0) == 2.75
    assert state.reinforce(5.0) == FactSalience(3.0, 10.0)


def test_normalized_salience_scales_the_decaying_bonus() -> None:
    state = FactSalience(3.0, 0.0)
    assert state.normalized_at(0.0) == 1.0
    assert state.normalized_at(_HALF_LIFE) == 0.5
    assert state.normalized_at(2 * _HALF_LIFE) == 0.25


@pytest.mark.parametrize(
    ("salience", "last_access"),
    (
        (0.5, 1.0),
        (4.0, 1.0),
        (True, 1.0),
        (float("nan"), 1.0),
        (1.5, None),
        (1.5, float("inf")),
    ),
)
def test_salience_rejects_invalid_state(
    salience: float, last_access: float | None
) -> None:
    with pytest.raises(DomainError):
        FactSalience(salience, last_access)
