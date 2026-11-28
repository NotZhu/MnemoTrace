"""验证有界分组证据和画像模型的发布契约。"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace

import httpx
import pytest

from mnemotrace.application.profile import (
    MAX_PROFILE_FACTS,
    ProfileEvidence,
    ProfileInputTooLarge,
    generate_profile,
)
from mnemotrace.application.profile_evidence import (
    ProfileFactState,
    changed_profile_evidence,
    profile_evidence_impact,
)
from mnemotrace.domain.enums import ProfileGroup, Source
from mnemotrace.domain.ids import EntityId, FactId
from mnemotrace.domain.profile import ProfileEntry
from mnemotrace.ports.inference import InvalidStructuredOutput
from tests.inference.sample import CHAT_ENV, chat_response, configured_chat
from tests.ports.fakes import FakeLLM

_BASIC = ProfileGroup.BASIC
_COMMUNICATION = ProfileGroup.COMMUNICATION_PREFERENCES
_CONSTRAINT = ProfileGroup.CONSTRAINTS


def test_profile_evidence_impact_at_effective_boundaries() -> None:
    subject = EntityId("profile-user")
    before = ProfileFactState(subject, Source.EXTRACTED, False, (_BASIC,), 100.0, 120.0)
    later = replace(before, valid_at=110.0)
    created = changed_profile_evidence(
        ((None, before), (None, later), (before, before)), subject
    )
    future = profile_evidence_impact(created, at=99.0)
    assert future.dirty == set()
    assert future.expiries == []
    assert future.transitions == {_BASIC: 100.0}

    active = profile_evidence_impact(created, at=100.0)
    assert active.dirty == {_BASIC}
    assert active.transitions == {_BASIC: 110.0}

    moved = replace(before, groups=(_CONSTRAINT,))
    reclassified = changed_profile_evidence(((before, moved),), subject)
    assert profile_evidence_impact(reclassified, at=100.0).dirty == {
        _BASIC,
        _CONSTRAINT,
    }
    for after in (None, moved, replace(before, sensitive=True)):
        changes = changed_profile_evidence(((before, after),), subject)
        expired = profile_evidence_impact(changes, at=120.0)
        assert expired.dirty == set()
        assert expired.expiries == [(frozenset({_BASIC}), 120.0)]
        assert expired.transitions == {}


class _ProfileLLM(FakeLLM):
    def __init__(self, response: Mapping[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((system, prompt, schema))
        return self.response


def _fact(identifier: str, text: str = "合成偏好") -> ProfileEvidence:
    return ProfileEvidence(FactId(identifier), "language", text, 80.0, None)


def _generate(
    llm: _ProfileLLM, evidence: Mapping[ProfileGroup, Sequence[ProfileEvidence]]
) -> dict[ProfileGroup, tuple[ProfileEntry, ...]]:
    return asyncio.run(generate_profile(llm, evidence))


def test_profile_sends_shared_evidence_once_and_preserves_group_references() -> None:
    default = _fact("default", "默认使用中文")
    exception = _fact("exception", "涉外邮件必须用英文")
    llm = _ProfileLLM(
        {
            _COMMUNICATION: [
                {"text": "默认用中文，涉外邮件例外", "evidence_numbers": [1, 2]}
            ],
            _CONSTRAINT: [{"text": "涉外邮件必须用英文", "evidence_numbers": [2]}],
        }
    )
    result = _generate(
        llm, {_COMMUNICATION: (default, exception), _CONSTRAINT: (exception,)}
    )
    assert result == {
        _COMMUNICATION: (
            ProfileEntry("默认用中文，涉外邮件例外", (default.id, exception.id)),
        ),
        _CONSTRAINT: (ProfileEntry("涉外邮件必须用英文", (exception.id,)),),
    }
    [(_, prompt, schema)] = llm.calls
    assert schema["required"] == [_COMMUNICATION, _CONSTRAINT]
    rows = json.loads(prompt)["facts"]
    assert [row["text"] for row in rows] == [default.text, exception.text]
    assert rows[1]["profile_groups"] == [_COMMUNICATION, _CONSTRAINT]


def test_profile_rejects_references_from_another_group() -> None:
    model = _ProfileLLM(
        {_BASIC: [{"text": "无依据", "evidence_numbers": [2]}], _CONSTRAINT: []}
    )
    with pytest.raises(InvalidStructuredOutput, match="分组以外"):
        _generate(model, {_BASIC: (_fact("basic"),), _CONSTRAINT: (_fact("rule"),)})


def test_profile_skips_generation_for_a_group_without_evidence() -> None:
    llm = _ProfileLLM({_BASIC: []})
    assert _generate(llm, {_BASIC: (_fact("new"),), _CONSTRAINT: ()}) == {
        _BASIC: (),
        _CONSTRAINT: (),
    }
    assert llm.calls[0][2]["required"] == [_BASIC]
    assert _generate(llm, {_BASIC: ()}) == {_BASIC: ()}
    assert len(llm.calls) == 1


def test_profile_checks_group_row_limit_before_inference() -> None:
    llm = _ProfileLLM({_BASIC: []})
    facts = tuple(_fact(str(index)) for index in range(MAX_PROFILE_FACTS + 1))
    assert _generate(llm, {_BASIC: facts[:-1]}) == {_BASIC: ()}
    with pytest.raises(ProfileInputTooLarge, match="条数"):
        _generate(llm, {_BASIC: facts})
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"basic": [], "constraints": []},
        {"basic": {}},
        {"basic": ["内容"]},
        {"basic": [{"text": "内容", "evidence_numbers": [1], "extra": 0}]},
        {"basic": [{"text": 15, "evidence_numbers": [1]}]},
        {"basic": [{"text": "  ", "evidence_numbers": [1]}]},
        {"basic": [{"text": "甲\x00", "evidence_numbers": [1]}]},
        {"basic": [{"text": "甲\ud800", "evidence_numbers": [1]}]},
        {"basic": [{"text": "内容", "evidence_numbers": []}]},
        {"basic": [{"text": "内容", "evidence_numbers": [True]}]},
        {"basic": [{"text": "内容", "evidence_numbers": [1.5]}]},
        {"basic": [{"text": "内容", "evidence_numbers": [0]}]},
    ],
)
def test_profile_rejects_malformed_response(response: Mapping[str, object]) -> None:
    with pytest.raises(InvalidStructuredOutput):
        _generate(_ProfileLLM(response), {_BASIC: (_fact("new"),)})


def test_profile_counts_trimmed_unicode_text_in_each_group() -> None:
    fact = _fact("new")
    text = "甲" * 300
    llm = _ProfileLLM({_BASIC: [{"text": f" {text} ", "evidence_numbers": [1, 1.0]}]})
    assert _generate(llm, {_BASIC: (fact,)}) == {
        _BASIC: (ProfileEntry(text, (fact.id,)),)
    }
    llm.response = {_BASIC: [{"text": text + "甲", "evidence_numbers": [1]}]}
    with pytest.raises(InvalidStructuredOutput, match="300"):
        _generate(llm, {_BASIC: (fact,)})


@pytest.mark.parametrize("oversized", [False, True])
def test_profile_uses_configured_model_budget(oversized: bool) -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response('{"basic":[{"text":"称呼阿宁。","evidence_numbers":[1]}]}')

    async def exercise() -> None:
        evidence = _fact("name", "中" * 4000 if oversized else "称呼阿宁。")
        env = {**CHAT_ENV, "MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS": "8192"}
        async with configured_chat(respond, env) as llm:
            if oversized:
                with pytest.raises(ProfileInputTooLarge, match="上下文"):
                    await generate_profile(llm, {_BASIC: (evidence,)})
            else:
                assert await generate_profile(llm, {_BASIC: (evidence,)}) == {
                    _BASIC: (ProfileEntry("称呼阿宁。", (evidence.id,)),)
                }
        assert calls == int(not oversized)

    asyncio.run(exercise())
