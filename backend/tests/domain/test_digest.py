"""验证固定来源摘要的字符限制及快照更新。"""

from dataclasses import replace

import pytest

from mnemotrace_next.domain.digest import Digest
from mnemotrace_next.domain.errors import InvalidDigest
from mnemotrace_next.domain.ids import DigestId, MessageId, SessionId, SpaceId


@pytest.fixture
def digest() -> Digest:
    """为摘要测试提供固定消息组。

    Returns:
        由含重复来源的输入构造的摘要快照。
    """
    return Digest(
        DigestId("digest"),
        SpaceId("space"),
        SessionId("session"),
        "第一次摘要",
        (MessageId("m2"), MessageId("m1"), MessageId("m2")),
        7,
        10,
    )


def test_digest_preserves_source_order_and_accepts_2000_characters(
    digest: Digest,
) -> None:
    at_limit = replace(digest, text="摘" * 2000)

    assert len(at_limit.text) == 2000
    assert at_limit.source_message_ids == (MessageId("m2"), MessageId("m1"))
    assert type(at_limit.created_at) is float
    assert type(at_limit.updated_at) is float


@pytest.mark.parametrize(
    "changes",
    (
        {"id": "digest"},
        {"session_id": SpaceId("session")},
        {"text": " \t"},
        {"text": "摘" * 2000 + " "},
        {"source_message_ids": ()},
        {"source_message_ids": ("m1",)},
        {"updated_at": 6},
    ),
)
def test_digest_rejects_invalid_fields(
    digest: Digest, changes: dict[str, object]
) -> None:
    with pytest.raises(InvalidDigest):
        replace(digest, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("at", (10, 11))
def test_digest_update_returns_a_snapshot_with_the_same_sources(
    digest: Digest, at: float
) -> None:
    updated = digest.update("修订摘要", at)

    assert updated is not digest
    assert updated == replace(digest, text="修订摘要", updated_at=at)
    assert digest.text == "第一次摘要"
    assert digest.updated_at == 10.0


def test_digest_rejects_a_stale_update_without_changing_the_snapshot(
    digest: Digest,
) -> None:
    with pytest.raises(InvalidDigest):
        digest.update("过早的修订", 9)
    with pytest.raises(InvalidDigest):
        digest.update("\t", 11)
    assert digest.text == "第一次摘要"
    assert digest.updated_at == 10.0
