"""验证画像生成用例的证据、预算和输出契约。"""

import asyncio
import json
from collections.abc import Mapping, Sequence

import httpx
import pytest

from mnemotrace.application.profile import ProfileInputTooLarge, generate_profile
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Source
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import EntityId, FactId, MessageId, SpaceId
from mnemotrace.domain.profile import ProfileEntry
from mnemotrace.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace.ports.inference import InvalidStructuredOutput
from tests.inference.sample import chat_response, configured_chat
from tests.ports.fakes import FakeLLM

_SPACE = SpaceId("profile")
_AT = 100.0
_EMPTY: dict[str, list[object]] = {
    "basic": [],
    "communication_preferences": [],
    "preferences": [],
    "constraints": [],
}


def _fact(
    identifier: str,
    *,
    predicate: str = "reply_style",
    text: str | None = None,
    space_id: SpaceId = _SPACE,
    subject_id: EntityId | None = None,
    valid_at: float | None = 80.0,
    invalid_at: float | None = None,
    recorded_at: float = 80.0,
    sensitive: bool = False,
    source: Source = Source.EXTRACTED,
) -> Fact:
    text = identifier if text is None else text
    return Fact(
        id=FactId(identifier),
        space_id=space_id,
        subject=EntityRef(subject_id or Entity.space_user(space_id).id),
        predicate=Predicate(predicate),
        object=LiteralValue(identifier),
        text=text,
        valid_at=valid_at,
        invalid_at=invalid_at,
        recorded_at=recorded_at,
        expired_at=max(recorded_at, invalid_at) if invalid_at is not None else None,
        provenance=(MessageId(identifier),) if source is Source.EXTRACTED else (),
        labels=(),
        sensitive=sensitive,
        source=source,
    )


class _ProfileLLM(FakeLLM):
    def __init__(self, response: Mapping[str, object]) -> None:
        self._response = response
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((system, prompt, schema))
        return self._response


def _generate(
    llm: _ProfileLLM,
    facts: Sequence[Fact],
    *,
    changed: Sequence[FactId] = (),
    previous: Sequence[FactId] = (),
) -> dict[str, tuple[ProfileEntry, ...]]:
    return asyncio.run(
        generate_profile(
            llm,
            space_id=_SPACE,
            facts=facts,
            reference_time=_AT,
            changed_fact_ids=changed,
            previous_fact_ids=previous,
        )
    )


def test_profile_filters_evidence_and_keeps_all_supporting_fact_ids() -> None:
    """有效的明确用户事实可以共同支撑一条沟通偏好。"""
    conclusion = "偏好先给结论，再说明理由。"
    llm = _ProfileLLM(
        {
            **_EMPTY,
            "communication_preferences": [
                {"text": f" {conclusion} ", "evidence_numbers": [1, 2]}
            ],
        }
    )
    latest = _fact("conclusion", text="用户要求以后先给结论。", valid_at=_AT)
    unknown = _fact("reason", text="用户要求回答时说明理由。", valid_at=None)
    actual = asyncio.run(
        generate_profile(
            llm,
            space_id=_SPACE,
            reference_time=_AT,
            facts=(
                unknown,
                _fact("other-space", space_id=SpaceId("another")),
                _fact("namesake", subject_id=Entity(_SPACE, "用户").id),
                _fact("private", sensitive=True),
                _fact("expired", invalid_at=_AT),
                _fact("future", valid_at=_AT + 1),
                _fact("manual", source=Source.MANUAL),
                latest,
            ),
        )
    )
    assert actual == {
        "basic": (),
        "communication_preferences": (
            ProfileEntry(conclusion, (latest.id, unknown.id)),
        ),
        "preferences": (),
        "constraints": (),
    }
    [(system, prompt, schema)] = llm.calls
    assert [(row["number"], row["text"]) for row in json.loads(prompt)["facts"]] == [
        (1, latest.text),
        (2, unknown.text),
    ]
    assert schema["required"] == list(_EMPTY)
    assert "省略当次要求" in system


def test_profile_skips_inference_when_no_evidence_is_eligible() -> None:
    llm = _ProfileLLM(_EMPTY)
    assert _generate(llm, (_fact("private", sensitive=True),)) == {
        group: () for group in _EMPTY
    }
    assert llm.calls == []


def test_profile_alternates_changed_and_previous_groups_deterministically() -> None:
    changed = _fact("changed", predicate="language", valid_at=85.0)
    same_predicate = _fact("same-group", predicate="language", valid_at=90.0)
    other_changed = _fact("other-changed", predicate="tool", valid_at=88.0)
    previous = _fact("previous", predicate="work_style", valid_at=None)
    recent = _fact("recent", predicate="timezone", valid_at=99.0)
    earlier = _fact("z-earlier", predicate="name", valid_at=99.0, recorded_at=90.0)
    tied = _fact("a-tied", predicate="format", valid_at=99.0, recorded_at=90.0)
    newer = _fact("newer", predicate="role", valid_at=99.0, recorded_at=98.0)
    invalid = _fact("expired", predicate="role", invalid_at=_AT)
    facts = (
        changed,
        same_predicate,
        other_changed,
        previous,
        recent,
        earlier,
        tied,
        newer,
        invalid,
    )
    llm = _ProfileLLM(_EMPTY)
    for ordered_facts in (facts, tuple(reversed(facts))):
        _generate(
            llm,
            ordered_facts,
            changed=(changed.id, other_changed.id),
            previous=(same_predicate.id, previous.id, invalid.id),
        )

    [first, second] = llm.calls
    assert first == second
    assert [row["text"] for row in json.loads(first[1])["facts"]] == [
        fact.text
        for fact in (
            other_changed,
            previous,
            same_predicate,
            changed,
            newer,
            tied,
            earlier,
            recent,
        )
    ]


def test_profile_budget_keeps_predicate_groups_whole_and_numbers_sent_evidence() -> (
    None
):
    small = _fact("language", predicate="language", text="优" * 3_000, valid_at=99.0)
    conflict = _fact("conflict", predicate="language", text="劣" * 8_000)
    changed = _fact("changed", predicate="format", text="详" * 4_850)
    previous = _fact("previous", predicate="style", text="短" * 4_850)
    overflow = _fact("overflow", predicate="tool", text="乙" * 3_000)
    tail = _fact("tail", predicate="scope")
    facts = (small, conflict, changed, previous, overflow, tail)
    llm = _ProfileLLM(
        {**_EMPTY, "preferences": [{"text": "工作偏好", "evidence_numbers": [1, 3]}]}
    )
    result = _generate(
        llm, facts, changed=(small.id, changed.id), previous=(previous.id,)
    )

    [(system, prompt, schema)] = llm.calls
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    assert len(system) + len(prompt) + len(schema_text) <= 12_000
    assert [(row["number"], row["text"]) for row in json.loads(prompt)["facts"]] == [
        (1, previous.text),
        (2, changed.text),
        (3, tail.text),
    ]
    assert result["preferences"] == (ProfileEntry("工作偏好", (previous.id, tail.id)),)

    invalid_llm = _ProfileLLM(
        {**_EMPTY, "preferences": [{"text": "工作偏好", "evidence_numbers": [4]}]}
    )
    with pytest.raises(InvalidStructuredOutput, match="事实编号"):
        _generate(
            invalid_llm, facts, changed=(small.id, changed.id), previous=(previous.id,)
        )


def test_profile_reports_a_budget_that_excludes_every_group() -> None:
    llm = _ProfileLLM(_EMPTY)
    with pytest.raises(ProfileInputTooLarge):
        _generate(llm, (_fact("oversized", text="长" * 12_000),))
    assert llm.calls == []


def test_profile_counts_json_escapes_at_the_exact_input_limit() -> None:
    sample = _ProfileLLM(_EMPTY)
    _generate(sample, (_fact("one", text="甲"),))
    [(system, prompt, schema)] = sample.calls
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    used = len(system) + len(prompt) + len(schema_text)

    llm = _ProfileLLM(_EMPTY)
    exact_text = '甲"' + "乙" * (12_000 - used - 2)
    _generate(llm, (_fact("one", text=exact_text),))
    [(_, at_limit, _)] = llm.calls
    assert len(system) + len(schema_text) + len(at_limit) == 12_000

    too_large = _ProfileLLM(_EMPTY)
    with pytest.raises(ProfileInputTooLarge):
        _generate(too_large, (_fact("one", text=exact_text + "乙"),))
    assert too_large.calls == []


@pytest.mark.parametrize(
    "response",
    (
        {"basic": []},
        {**_EMPTY, "unknown": []},
        {**_EMPTY, "basic": {}},
        {**_EMPTY, "basic": ["内容"]},
        {**_EMPTY, "basic": [{"text": "内容", "evidence_numbers": [1], "extra": 0}]},
        {**_EMPTY, "basic": [{"text": 15, "evidence_numbers": [1]}]},
        {**_EMPTY, "basic": [{"text": "  ", "evidence_numbers": [1]}]},
        {**_EMPTY, "basic": [{"text": "甲\x00", "evidence_numbers": [1]}]},
        {**_EMPTY, "basic": [{"text": "甲\ud800", "evidence_numbers": [1]}]},
        {**_EMPTY, "basic": [{"text": "内容", "evidence_numbers": []}]},
        {**_EMPTY, "basic": [{"text": "内容", "evidence_numbers": [True]}]},
        {**_EMPTY, "basic": [{"text": "内容", "evidence_numbers": [1.5]}]},
        {**_EMPTY, "basic": [{"text": "内容", "evidence_numbers": [0]}]},
    ),
)
def test_profile_rejects_a_malformed_response(response: Mapping[str, object]) -> None:
    llm = _ProfileLLM(response)
    with pytest.raises(InvalidStructuredOutput):
        _generate(llm, (_fact("one"), _fact("two")))
    assert len(llm.calls) == 1


def test_profile_counts_trimmed_unicode_text_across_groups() -> None:
    fact = _fact("one")
    at_limit = {
        **_EMPTY,
        "basic": [{"text": f" {'甲' * 600} ", "evidence_numbers": [1, 1.0]}],
        "constraints": [{"text": "📌" * 600, "evidence_numbers": [1]}],
    }
    result = _generate(_ProfileLLM(at_limit), (fact,))
    assert result["basic"] == (ProfileEntry("甲" * 600, (fact.id,)),)
    assert result["constraints"] == (ProfileEntry("📌" * 600, (fact.id,)),)

    over_limit = {
        **at_limit,
        "preferences": [{"text": "甲", "evidence_numbers": [1]}],
    }
    with pytest.raises(InvalidStructuredOutput, match="1200"):
        _generate(_ProfileLLM(over_limit), (fact,))


def test_profile_supplies_conflicts_and_scoped_preferences_as_evidence() -> None:
    facts = (
        _fact("a", predicate="language", text="默认用中文。"),
        _fact("b", predicate="language", text="默认用英文。"),
        _fact("c", predicate="language", text="写客户邮件时，明确要求用英文。"),
        _fact("d", predicate="language", text="写团队周报时，明确要求用中文。"),
        _fact("e", predicate="length", text="这次回复简短一些。"),
    )
    scoped = "客户邮件用英文，团队周报用中文。"
    llm = _ProfileLLM(
        {
            **_EMPTY,
            "communication_preferences": [{"text": scoped, "evidence_numbers": [3, 4]}],
        }
    )
    result = _generate(llm, facts)

    [(system, prompt, _)] = llm.calls
    assert "同一适用范围内存在无法解决的冲突时省略对应条目" in system
    assert "适用条件不同的偏好可以并存" in system
    assert [row["text"] for row in json.loads(prompt)["facts"]] == [
        fact.text for fact in facts
    ]
    assert result["communication_preferences"] == (
        ProfileEntry(scoped, (facts[2].id, facts[3].id)),
    )


def test_profile_schema_works_with_the_configured_chat_adapter() -> None:
    fact = _fact("name", predicate="name", text="以后请称我为阿宁。")
    response = {**_EMPTY, "basic": [{"text": "称呼阿宁。", "evidence_numbers": [1]}]}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return chat_response(json.dumps(response, ensure_ascii=False))

    async def generate() -> dict[str, tuple[ProfileEntry, ...]]:
        async with configured_chat(respond) as llm:
            return await generate_profile(
                llm, space_id=_SPACE, facts=(fact,), reference_time=_AT
            )

    assert asyncio.run(generate())["basic"] == (ProfileEntry("称呼阿宁。", (fact.id,)),)
    [request] = requests
    assert json.loads(request.content)["response_format"]["type"] == "json_schema"
