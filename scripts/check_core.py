#!/usr/bin/env python3
"""Verify the standard-library core without making model or network calls."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from mnemotrace import MemoryEngine, LLMNotConfiguredError
    from mnemotrace.service import MemoryService
    from mnemotrace.server.openai_compat import latest_user_text

    assert not hasattr(MemoryService, 'import_')
    assert latest_user_text([{'role': 'user', 'content': 'Synthetic text'}]) == 'Synthetic text'
    subprocess.run([sys.executable, '-c', """
import sys
from mnemotrace.service import MemoryService
from mnemotrace.server.openai_compat import latest_user_text
assert not any(name.startswith('mnemotrace.connectors') for name in sys.modules)
"""], cwd=ROOT, check=True)

    mem = MemoryEngine()
    mem.add_fact('user', 'lives_in', 'Paris', text='The user lives in Paris.')
    mem.add('Synthetic pending record')
    raw = MemoryEngine()
    saved = raw.remember("Synthetic note", extract=False)
    assert raw.episodes_doc.get(saved["episode_id"]).source_kind == "note"
    accepted = raw.remember_messages(
        [{"message_id": "m1", "role": "user", "content": "Synthetic dialogue"}], "chat", extract=False)
    assert accepted["accepted"] == 1 and not raw.fact_store.values()
    assert mem.search('Where do I live?').top().object == 'Paris'
    try:
        mem.consolidate()
    except LLMNotConfiguredError:
        pass
    else:
        raise AssertionError('Extraction must require an LLM')
    for module in ('mnemotrace.admin', 'mnemotrace.agent_doctor', 'mnemotrace.agent_setup',
                   'mnemotrace.mcp', 'mnemotrace.worker'):
        subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT,
                       check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, '-c',
                    'from mnemotrace.agent_setup import bootstrap_main; bootstrap_main(["--help"])'],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    # An empty environment must produce an actionable configuration error, not a demo.
    env = {k: v for k, v in os.environ.items() if not k.startswith('MNEMOTRACE_')}
    # load_dotenv uses setdefault: an explicit empty value also blocks repository .env.
    env['MNEMOTRACE_LLM'] = ''
    result = subprocess.run([sys.executable, '-m', 'mnemotrace.quickstart'], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'MNEMOTRACE_LLM' in result.stderr
    print('OK offline core verification passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
