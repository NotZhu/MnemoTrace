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

    provenance_chunk_cfg = mnemotrace_config(ablations=("provenance_chunks",))
    assert provenance_chunk_cfg.provenance_evidence is True
    assert provenance_chunk_cfg.provenance_chunk_promotion is False

    relation_cfg = mnemotrace_config(ablations=("graph_relation",))
    assert relation_cfg.graph_proximity is True
    assert relation_cfg.graph_relation_awareness is False

    reinforcement_cfg = mnemotrace_config(ablations=("graph_reinforcement",))
    assert reinforcement_cfg.graph_proximity is True
    assert reinforcement_cfg.graph_path_reinforcement is False


def test_bench_exposes_named_lean_ablation_systems():
    names = {
        "engram_lean_no_chain",
        "engram_lean_no_raw",
        "engram_lean_no_provenance_chunks",
        "engram_lean_no_evidence_budget",
        "engram_lean_no_digest_fallback",
        "engram_lean_no_graph",
        "engram_lean_no_graph_relation",
        "engram_lean_no_graph_reinforcement",
        "engram_lean_core",
    }

    assert names <= set(SYSTEMS)
    assert SYSTEMS["engram_lean_no_chain"].ablations == ("chain",)
    assert SYSTEMS["engram_lean_no_raw"].ablations == ("raw",)
    assert SYSTEMS["engram_lean_no_provenance_chunks"].ablations == ("provenance_chunks",)
    assert SYSTEMS["engram_lean_no_evidence_budget"].ablations == ("evidence_budget",)
    assert SYSTEMS["engram_lean_no_digest_fallback"].ablations == ("digest_fallback",)
    assert SYSTEMS["engram_lean_no_graph"].ablations == ("graph",)
    assert SYSTEMS["engram_lean_no_graph_relation"].ablations == ("graph_relation",)
    assert SYSTEMS["engram_lean_no_graph_reinforcement"].ablations == ("graph_reinforcement",)
    assert SYSTEMS["engram_lean_core"].ablations == ("chain", "raw", "graph")


@pytest.mark.parametrize('flag', ['summary_fallback', 'typo'])
def test_bench_rejects_unknown_ablation_before_setup(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, 'argv', ['bench.py', '--ablate', f'chain,{flag}'])
    monkeypatch.setattr(bench, 'load_dotenv', lambda: pytest.fail('started setup'))
    with pytest.raises(SystemExit) as exc:
        bench.main()
    assert exc.value.code == 2
    assert f'Unknown ablation option(s): {flag}' in capsys.readouterr().err


def test_digest_fallback_ablation_distinguishes_enabled_and_disabled_evidence():
    rows, _ = run_ablation()
    result = next(row for row in rows if row.feature == "digest_fallback")
    assert result.enabled_hit
    assert not result.disabled_hit
    assert result.improved
