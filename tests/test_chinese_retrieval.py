
from tests.model_fixtures import seed_linked
import pytest

from mnemotrace import MemoryEngine
from mnemotrace.retrieve.hybrid import mentioned_entities
from mnemotrace.retrieve.lexical import bm25_scores, lexical_terms
from mnemotrace.types import Entity


def test_lexical_analysis_handles_han_latin_and_single_character_queries():
    assert lexical_terms('ＰｏｓｔｇｒｅＳＱＬ绿茶２０２５', query=True) == ['postgresql', '绿茶', '2025']
    assert lexical_terms('绿茶', exact=True) == ['绿', '茶', '绿茶']
    docs = [('match', '我喜欢喝绿茶'), ('other', '我喜欢喝咖啡')]
    for query in ('绿茶', '茶'):
        scores = bm25_scores(query, docs)
        assert scores['match'] > scores['other'] == 0


def test_entity_mentions_prefer_long_names():
    short = Entity(name='小明', user_id='u')
    long = Entity(name='王小明', user_id='u')
    assert list(mentioned_entities('查王小明', [short, long])) == [long.id]
    assert not mentioned_entities('查同事甲', [long, short])


def test_graph_does_not_stem_identifiers_into_entity_matches():
    memory = MemoryEngine()
    seed_linked(memory, 'Redis', 'based_in', 'Synthetic Location', user_id='u', text='Redis based in Synthetic Location')
    assert not memory.retriever.query_entity_ids('redi', 'u')
    assert memory.retriever.query_entity_ids('redis', 'u')


def test_lexical_memory_resolves_chinese_two_hop_fact_chain():
    memory = MemoryEngine()
    relationship = seed_linked(memory, 'user', 'sister', '王小明', text='user sister 王小明')
    employer = seed_linked(memory, '王小明', 'works_at', '月亮公司', text='王小明 works at 月亮公司')
    result = memory.follow_path('user', ['sister', 'works_at'])
    match, = result.paths
    assert [fact.id for fact in match.facts] == [relationship.id, employer.id]
    assert match.endpoint == '月亮公司'


@pytest.mark.parametrize('query', ['我的姐姐喜欢哪家公司？', '我的姐姐的公司在哪里？',
                                   '我 的 姐姐喜欢哪家公司？'])
def test_chinese_two_hop_does_not_infer_employment_from_company(query):
    memory = MemoryEngine()
    memory.add_fact('user', 'sister', 'Ana', text='user sister Ana')
    memory.add_fact('Ana', 'works_at', 'Orbit', text='Ana works at Orbit')
    memory.add_fact('Ana', 'likes', 'Nadir', text='Ana likes Nadir')
    memory.add_fact('Orbit', 'based_in', 'Taipei', text='Orbit based in Taipei')
    assert memory.search(query).via != 'multi-hop'


def test_digest_lexical_recall_handles_chinese_without_an_embedder():
    memory = MemoryEngine()
    source = memory.add('原文独有码甲。')
    source.digest = '我喜欢喝绿茶。'
    memory._sync_episode(source)
    hit, = memory.retrieve_digests('绿茶')
    assert hit.id == source.id
    assert not memory.retrieve_digests('独有码甲')
    assert not memory.retrieve_digests('What is Kryptonite?')


def test_search_prefers_a_literal_identifier_over_identical_lexical_terms():
    memory = MemoryEngine()
    exact = memory.add_fact('Project', 'reference', 'AB-104', valid_at=1, text='Project reference AB-104')
    memory.add_fact('Project', 'reference', 'AB/104', valid_at=2, text='Project reference AB/104')
    memory.add_fact('Project', 'reference', 'AB-104-B', valid_at=3, text='Project reference AB-104-B')
    assert memory.search('Find AB-104').top().id == exact.id
