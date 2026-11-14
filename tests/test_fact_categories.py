"""Fact metadata contracts, independent of the extraction model provider."""
import json
from dataclasses import asdict

import pytest

from mnemotrace import Fact, MemoryEngine
from mnemotrace.consolidate.operations import decode
from mnemotrace.extraction import ExtractionResponseError
from mnemotrace.config import Config
from mnemotrace.consolidate.facts import materialize_facts
from mnemotrace.store._validation import SNAPSHOT_FORMAT_VERSION


def window():
    source = dict(id="s1", role="user", event_time=100, sequence=1, needs_digest=False)
    return dict(operation="extract", source_kind="dialogue", context=[], pending=[source])


def item(**patch):
    return dict(subject="我", predicate="prefers", object="decaf when working late",
                text="I prefer decaf when working late", source_ids=["s1"],
                categories=["food", "user_preferences", "food"], sensitive=False, time_kind="current", effective_date=None, retention="unknown") | patch


def extract(rows):
    data = window()
    product = decode(json.dumps(dict(facts=rows, digests=[])), data, Config())
    return materialize_facts("account", data, product, 101)


def test_model_facts_preserve_subject_name_and_qualified_preferences():
    name, friend, preference = extract([
        item(predicate="name", object="Jules", text="My name is Jules", categories=["personal_details"]),
        item(subject="Jules", object="jazz", text="My friend Jules prefers jazz"),
        item(),
    ])
    assert (name.subject, name.object) == ("我", "Jules")
    assert friend.subject == "Jules"
    assert (preference.subject, preference.object, preference.text) == (
        "我", "decaf when working late", "I prefer decaf when working late")
    assert (preference.categories, preference.object_id) == (
        ["food", "user_preferences"], None)


@pytest.mark.parametrize("labels", [[], ["misc"]])
def test_empty_and_misc_are_distinct(labels):
    fact, = extract([item(categories=labels)])
    assert fact.categories == labels


@pytest.mark.parametrize("patch", [
    {"categories": "food"}, {"categories": ["bogus"]}, {"categories": [["food"]]},
    {"object_kind": "text"}, {"source_ids": ["missing"]}, {"categories_source": "user"},
    {"subject": "  "},
])
def test_invalid_model_metadata_is_rejected(patch):
    with pytest.raises(ExtractionResponseError):
        extract([item(**patch)])


def test_sensitivity_changes_without_reclassifying_facts():
    memory = MemoryEngine()
    fact = memory.add_fact("我", "prefers", "tea", categories=["food", "food"],
                           sensitive=False, text="user prefers tea")
    memory.set_fact_sensitive(fact.id, True)
    updated = memory.fact_store.get(fact.id)
    assert (updated.categories, updated.sensitive) == (["food"], True)


def test_snapshot_preserves_metadata_and_checks_version():
    fact = Fact("我", "prefers", "tea", categories=["food"], text='user prefers tea')
    snapshot = dict(format_version=SNAPSHOT_FORMAT_VERSION, facts=[asdict(fact)], episodes=[])
    memory = MemoryEngine()
    memory.import_snapshot(snapshot)
    restored, = memory.fact_store.values()
    assert (restored.categories, restored.object_id) == (
        ["food"], None)
    with pytest.raises(ValueError, match="unsupported snapshot version"):
        memory.import_snapshot({**snapshot, "format_version": 1})
