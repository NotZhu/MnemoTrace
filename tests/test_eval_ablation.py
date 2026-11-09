import sys

import pytest

from eval import ablate_features, bench
from eval.bench import SYSTEMS, mnemotrace_config
from tests.model_fixtures import RecordedLLM


def run_ablation():
    return ablate_features.run_ablation(llm=RecordedLLM())


def test_mnemotrace_config_applies_algorithm_ablation_flags():
    cfg = mnemotrace_config(ablations=("chain", "raw", "graph"))
    assert not cfg.chain_evidence and not cfg.provenance_evidence and not cfg.graph_proximity
    assert cfg.graph_relation_awareness and cfg.evidence_budgeting

    budget_cfg = mnemotrace_config(ablations=("evidence_budget",))
    assert budget_cfg.evidence_budgeting is False

    digest_cfg = mnemotrace_config(ablations=("digest_fallback",))
    assert digest_cfg.digest_fallback is False

    procedural_extraction_cfg = mnemotrace_config(ablations=("procedural_extraction",))
    assert procedural_extraction_cfg.procedural_extraction is False

    preference_extraction_cfg = mnemotrace_config(ablations=("explicit_preference_extraction",))
    assert preference_extraction_cfg.explicit_preference_extraction is False

    preference_filter_cfg = mnemotrace_config(ablations=("preference_object_filter",))
    assert preference_filter_cfg.preference_object_filter is False

    preference_norm_cfg = mnemotrace_config(ablations=("preference_object_normalization",))
    assert preference_norm_cfg.preference_object_normalization is False

    preference_reversal_cfg = mnemotrace_config(ablations=("preference_reversal_extraction",))
    assert preference_reversal_cfg.preference_reversal_extraction is False

    provenance_chunk_cfg = mnemotrace_config(ablations=("provenance_chunks",))
    assert provenance_chunk_cfg.provenance_evidence is True
    assert provenance_chunk_cfg.provenance_chunk_promotion is False

    relation_cfg = mnemotrace_config(ablations=("graph_relation",))
    assert relation_cfg.graph_proximity is True
    assert relation_cfg.graph_relation_awareness is False

    reinforcement_cfg = mnemotrace_config(ablations=("graph_reinforcement",))
    assert reinforcement_cfg.graph_proximity is True
    assert reinforcement_cfg.graph_path_reinforcement is False

    self_anchor_cfg = mnemotrace_config(ablations=("graph_self_anchor",))
    assert self_anchor_cfg.graph_proximity is True
    assert self_anchor_cfg.graph_self_anchor is False


def test_bench_exposes_named_lean_ablation_systems():
    names = {
        "engram_lean_no_chain",
        "engram_lean_no_raw",
        "engram_lean_no_provenance_chunks",
        "engram_lean_no_evidence_budget",
        "engram_lean_no_digest_fallback",
        "engram_lean_no_procedural_extraction",
        "engram_lean_no_explicit_preference_extraction",
        "engram_lean_no_preference_object_filter",
        "engram_lean_no_preference_object_normalization",
        "engram_lean_no_preference_reversal_extraction",
        "engram_lean_no_graph",
        "engram_lean_no_graph_relation",
        "engram_lean_no_graph_reinforcement",
        "engram_lean_no_graph_self_anchor",
        "engram_lean_core",
    }

    assert names <= set(SYSTEMS)
    assert SYSTEMS["engram_lean_no_chain"].ablations == ("chain",)
    assert SYSTEMS["engram_lean_no_raw"].ablations == ("raw",)
    assert SYSTEMS["engram_lean_no_provenance_chunks"].ablations == ("provenance_chunks",)
    assert SYSTEMS["engram_lean_no_evidence_budget"].ablations == ("evidence_budget",)
    assert SYSTEMS["engram_lean_no_digest_fallback"].ablations == ("digest_fallback",)
    assert SYSTEMS["engram_lean_no_procedural_extraction"].ablations == ("procedural_extraction",)
    assert SYSTEMS["engram_lean_no_explicit_preference_extraction"].ablations == (
        "explicit_preference_extraction",
    )
    assert SYSTEMS["engram_lean_no_preference_object_filter"].ablations == ("preference_object_filter",)
    assert SYSTEMS["engram_lean_no_preference_object_normalization"].ablations == (
        "preference_object_normalization",
    )
    assert SYSTEMS["engram_lean_no_preference_reversal_extraction"].ablations == (
        "preference_reversal_extraction",
    )
    assert SYSTEMS["engram_lean_no_graph"].ablations == ("graph",)
    assert SYSTEMS["engram_lean_no_graph_relation"].ablations == ("graph_relation",)
    assert SYSTEMS["engram_lean_no_graph_reinforcement"].ablations == ("graph_reinforcement",)
    assert SYSTEMS["engram_lean_no_graph_self_anchor"].ablations == ("graph_self_anchor",)
    assert SYSTEMS["engram_lean_core"].ablations == ("chain", "raw", "graph")


@pytest.mark.parametrize('flag', ['summary_fallback', 'typo'])
def test_bench_rejects_unknown_ablation_before_setup(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, 'argv', ['bench.py', '--ablate', f'chain,{flag}'])
    monkeypatch.setattr(bench, 'load_dotenv', lambda: pytest.fail('started setup'))
    with pytest.raises(SystemExit) as exc:
        bench.main()
    assert exc.value.code == 2
    assert f'Unknown ablation option(s): {flag}' in capsys.readouterr().err


def test_offline_feature_ablation_proves_each_enabled_feature_changes_target_evidence():
    rows, digest = run_ablation()


@pytest.mark.parametrize("context, expected_hit", [
    ("当前偏好记录（结构化）：\naisle seats\nred-eye flights", True),
    ("PREFERENCE RECORDS (current, structured):\naisle seats\nred-eye flights", False),
    ("RELEVANT CONVERSATIONS (full detail):\naisle seats\nred-eye flights", False),
    ("当前偏好记录（结构化）：\naisle seats", False),
    ("PREFERENCE RECORDS (current, structured):\nred-eye flights", False),
])
def test_preference_ablation_requires_structured_block_and_both_values(
    monkeypatch, context, expected_hit
):
    monkeypatch.setattr(
        ablate_features,
        "_explicit_preference_extraction_context",
        lambda enabled: context if enabled else "",
    )

    rows, _ = run_ablation()
    preference = next(row for row in rows if row.feature == "explicit_preference_extraction")

    assert preference.enabled_hit is expected_hit
    assert preference.disabled_hit is False
    assert preference.improved is expected_hit
