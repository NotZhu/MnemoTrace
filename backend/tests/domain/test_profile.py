"""验证画像条目的来源及快照隔离。"""

from collections.abc import Hashable
from dataclasses import FrozenInstanceError, replace

import pytest

from mnemotrace_next.domain.errors import DomainError, InvalidProfile
from mnemotrace_next.domain.ids import FactId, SessionId, SpaceId
from mnemotrace_next.domain.profile import Profile, ProfileEntry


@pytest.fixture
def profile() -> Profile:
    """为画像替换和校验测试提供初始快照。

    Returns:
        包含单条文字及其来源事实的画像。
    """
    return Profile(
        SpaceId("space"), {"basic": (ProfileEntry("喝茶", (FactId("f1"),)),)}, 10
    )


def test_profile_freezes_the_mapping_and_derives_ordered_fact_ids() -> None:
    first = ProfileEntry("喝茶", (FactId("f2"), FactId("f1"), FactId("f2")))
    second = ProfileEntry("需要茶具", (FactId("f1"), FactId("f3")))
    data = {"interests": (first,), "basic": (second,), "empty": ()}

    profile = Profile(SpaceId("space"), data, 10)
    data.clear()

    assert profile.data == {"interests": (first,), "basic": (second,), "empty": ()}
    assert first.fact_ids == (FactId("f2"), FactId("f1"))
    assert profile.source_fact_ids == (FactId("f2"), FactId("f1"), FactId("f3"))
    assert type(profile.updated_at) is float
    with pytest.raises(TypeError):
        profile.data["interests"] = ()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        first.text = "其他"  # type: ignore[misc]


def test_profile_preserves_value_equality_and_is_unhashable(profile: Profile) -> None:
    assert profile == replace(profile)
    assert not isinstance(profile, Hashable)
    with pytest.raises(TypeError, match="unhashable type: 'Profile'"):
        hash(profile)


def test_profile_entry_requires_text_and_fact_ids() -> None:
    with pytest.raises(DomainError):
        ProfileEntry("\t", (FactId("f1"),))
    with pytest.raises(DomainError):
        ProfileEntry("喝茶", ())


@pytest.mark.parametrize(
    "changes",
    (
        {"space_id": SessionId("space")},
        {"data": {" ": ()}},
        {"data": {"basic": []}},
        {"data": {"basic": ("喝茶",)}},
        {"updated_at": None},
    ),
)
def test_profile_rejects_invalid_fields(
    profile: Profile, changes: dict[str, object]
) -> None:
    with pytest.raises(InvalidProfile):
        replace(profile, **changes)  # type: ignore[arg-type]


def test_profile_update_replaces_all_entries_and_rebuilds_sources(
    profile: Profile,
) -> None:
    entry = ProfileEntry("常去旅行", (FactId("f2"),))
    data = {"interests": (entry,)}
    updated = profile.update(data, 10)
    data.clear()

    assert updated is not profile
    assert updated.data == {"interests": (entry,)}
    assert updated.source_fact_ids == (FactId("f2"),)
    assert updated.space_id == profile.space_id
    assert updated.updated_at == 10.0
    assert profile.source_fact_ids == (FactId("f1"),)

    cleared = updated.update({}, 11)
    assert cleared.data == {}
    assert cleared.source_fact_ids == ()
    assert cleared.updated_at == 11.0


def test_profile_rejects_a_stale_or_invalid_update(profile: Profile) -> None:
    with pytest.raises(InvalidProfile):
        profile.update({}, 9)
    with pytest.raises(InvalidProfile):
        profile.update({"\t": ()}, 11)
    assert profile.source_fact_ids == (FactId("f1"),)
    assert profile.updated_at == 10.0
