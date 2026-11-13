"""Run synthetic quality and filtered vector workloads in disposable Postgres databases."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import gzip
from itertools import product
import json
import os
from pathlib import Path
import platform
import random
import time

from eval import harness
from eval.storage_baseline import _git, capture_sources, observe, workload_manifest, workload_records
from mnemotrace import Config
from mnemotrace.memory import Memory
from mnemotrace.store.contracts import FactQuery, FactState, RetrievalQuery, SpaceContext, SpaceSettings
from mnemotrace.store.postgres import Accounts, Database, Spaces
from mnemotrace.store.postgres.dao import PostgresDAO
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.migrate import _roles
from mnemotrace.store.postgres.reads import ReadView
from mnemotrace.store.postgres.records import FACT, domain, timestamp
from mnemotrace.store.postgres.retrieval import Corpus
from mnemotrace.retrieve.lexical import lexical_terms
from mnemotrace.util import DAY

INDEX = 'phase5_experiment_hnsw'


def quality(factory, out, *, processing='immediate'):
    from tests.model_fixtures import RecordedLLM

    ref_rows, ref_summary, reference = observe()
    harness.write_jsonl(str(out / 'reference.jsonl'), ref_rows, ref_summary)
    (out / 'reference-observation.json').write_text(json.dumps(reference, ensure_ascii=False), encoding='utf-8')
    with (factory() as bundle, Database(bundle.admin, purpose='admin') as admin,
          Database(bundle.runtime) as runtime):
        user = Accounts(admin).create_user()
        spaces = Spaces(runtime)

        def run_item(item, *, storage, llm):
            scope = spaces.authorize(user, spaces.create(user, item.id).space_id)
            memory = Memory(runtime, scope, llm=llm, config=Config(access_boost=0))
            origins = {}
            for index, (text, day) in enumerate(item.sessions, 1):
                accepted = memory.remember(text, 'eval', event_time=harness.BASE + day * DAY,
                                           processing=processing)
                expected = 'completed' if processing == 'immediate' else 'accepted'
                if accepted['status'] != expected:
                    raise RuntimeError(f'{item.id}: fixed-response ingestion failed')
                source_id, = accepted['source_ids']
                origins[source_id] = f'source-{index}'
            if processing == 'buffered':
                if not memory.flush_processing('eval')['ok']:
                    raise RuntimeError(f'{item.id}: fixed-response buffered processing failed')
                # A new pool must observe the same accepted sources and completed receipts.
                with Database(bundle.runtime) as reopened:
                    restored = Memory(reopened, scope)
                    if not all(restored.get_episode(source_id).consolidated for source_id in origins):
                        raise RuntimeError(f'{item.id}: completed sources missing after reopening')
            when = None if item.as_of_day is None else harness.BASE + item.as_of_day * DAY
            conditions = FactQuery(valid_at=when)
            start = time.perf_counter()
            if item.path:
                anchor = 'user' if item.path[0] == item.id else item.path[0]
                result = harness.path_answer(item, memory.follow_path(anchor, item.path[1:], facts=conditions))
            else:
                result = memory.search(item.question, facts=conditions)
            elapsed = 1000 * (time.perf_counter() - start)
            request = memory._query(item.question, facts=conditions, top_k=memory._config.top_k)
            with memory.dao.read_snapshot() as view:
                batch = view.candidates(request, config=memory._config)
            ranked = [dict(subject=f.subject, predicate=f.predicate, object=f.object, valid_at=f.valid_at,
                           provenance=[origins[key] for key in f.provenance]) for f in result.facts]
            candidates = [dict(subject=f.subject, predicate=f.predicate, object=f.object)
                          for f in (domain(item.record) for item in batch.candidates)]
            return dict(id=item.id, category=item.category, question=item.question, gold=item.answer,
                        storage=storage, ok=harness.score_search(item.answer, result),
                        answer=result.answer(), status=result.status,
                        top_fact_object=result.top().object if result.top() is not None else None,
                        abstained=result.abstained, latency_ms=elapsed,
                        tokens=len(lexical_terms(' '.join(fact.text for fact in result.facts))), via=result.via,
                        fact_ids=[fact.id for fact in result.facts], ranked=ranked, candidates=candidates,
                        limitations=result.limitations)

        rows, summary = harness.evaluate(storage='postgres', llm=RecordedLLM(), run_item=run_item)
        harness.write_jsonl(str(out / 'postgres.jsonl'), rows, summary)
        print(f"Quality: {summary['accuracy']:.1f}% Postgres, {ref_summary['accuracy']:.1f}% reference", flush=True)


class _WorkloadView(ReadView):
    def __init__(self, view, bucket):
        super().__init__(view._sql, view.snapshot, view.at, view._settings)
        self.bucket = bucket

    def _fact_conditions(self, query):
        conditions, args = super()._fact_conditions(query)
        conditions.append("""EXISTS (SELECT 1 FROM workload.filters b
            WHERE (b.user_id,b.space_id,b.fact_id) = (r.user_id,r.space_id,r.id) AND b.bucket < %s)""")
        args.append(self.bucket)
        return conditions, args


def _load_workload(bundle, manifest, case):
    from psycopg import sql
    from psycopg.types.json import Jsonb

    psycopg, _ = driver()
    start = time.perf_counter()
    with psycopg.connect(bundle.bootstrap.dsn) as conn:
        role = sql.Identifier(_roles(bundle.runtime.role_prefix)['runtime'])
        conn.execute('CREATE SCHEMA workload')
        conn.execute('CREATE TABLE workload.filters(user_id text, space_id text, fact_id text, bucket integer, '
                     'PRIMARY KEY(user_id,space_id,fact_id))')
        conn.execute('ALTER TABLE workload.filters ENABLE ROW LEVEL SECURITY')
        conn.execute("CREATE POLICY owned ON workload.filters USING (user_id = current_setting('mnemotrace.user_id', true))")
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA workload TO {}').format(role))
        conn.execute(sql.SQL('GRANT SELECT ON workload.filters TO {}').format(role))
        conn.execute("INSERT INTO mnemotrace.users(user_id,status) SELECT 'user-' || n,'active' FROM generate_series(0,9) n")
        conn.execute("""INSERT INTO mnemotrace.memory_spaces(space_id,user_id,name,status,settings)
            SELECT 'space-' || n,'user-' || mod(n,10),'Synthetic workload','active',%s
            FROM generate_series(0,%s) n""", (Jsonb(asdict(SpaceSettings())), case['spaces'] - 1))
        conn.execute("""CREATE TEMP TABLE input(user_id text,space_id text,id text,subject text,predicate text,
            object text,body text,valid_at timestamptz,invalid_at timestamptz,bucket integer,vector public.vector)""")
        with conn.cursor().copy('COPY input FROM STDIN') as copy:
            for row in workload_records(case['name']):
                vector = '[' + ','.join(map(str, row['vector'])) + ']'
                copy.write_row((row['user_id'], row['space_id'], row['id'], row['subject'], row['predicate'],
                                row['object'], row['text'], timestamp(row['valid_at']), timestamp(row['invalid_at']),
                                row['filter_bucket'], vector))
        conn.execute('INSERT INTO workload.filters SELECT user_id,space_id,id,bucket FROM input')
        conn.execute("""INSERT INTO mnemotrace.relation_types
            (user_id,space_id,id,predicate,meaning,direction,endpoint_types,record_version,content_version)
            SELECT DISTINCT user_id,space_id,'pred_' || encode(sha256(convert_to(predicate, 'UTF8')), 'hex'),
                predicate,predicate,'forward','[]',1,1 FROM input""")
        conn.execute("""INSERT INTO mnemotrace.facts (user_id,space_id,id,subject,predicate,object,text,
            salience,confidence,source,categories,sensitive,relation_id,categories_source,retention,source_roles,
            valid_at,invalid_at,created_at,last_access,access_count,record_version,content_version)
            SELECT user_id,space_id,id,subject,predicate,object,body,1,1,'user','{}',false,
                   'pred_' || encode(sha256(convert_to(predicate, 'UTF8')), 'hex'),
                   'unset','unknown','{}',valid_at,invalid_at,valid_at,valid_at,0,1,1 FROM input""")
        conn.execute("""INSERT INTO mnemotrace.embeddings(user_id,space_id,fact_id,target_kind,field,
            content_version,model,dimension,vector)
            SELECT user_id,space_id,id,'Fact','text',1,%s,%s,vector FROM input""",
            (manifest['model'], manifest['dimension']))
        conn.execute('ANALYZE mnemotrace.facts')
        conn.execute('ANALYZE mnemotrace.embeddings')
        conn.execute('ANALYZE workload.filters')
    return time.perf_counter() - start


def _create_hnsw(bundle, manifest):
    from psycopg import sql

    psycopg, _ = driver()
    start = time.perf_counter()
    with psycopg.connect(bundle.bootstrap.dsn) as conn:
        conn.execute("SET LOCAL maintenance_work_mem = '128MB'")
        conn.execute('SET LOCAL max_parallel_maintenance_workers = 0')
        conn.execute(sql.SQL("""CREATE INDEX {} ON mnemotrace.embeddings
            USING hnsw ((vector::public.vector({})) public.vector_cosine_ops)
            WHERE target_kind = 'Fact' AND field = 'text' AND model = {} AND dimension = {}""").format(
            sql.Identifier(INDEX), sql.Literal(manifest['dimension']), sql.Literal(manifest['model']),
            sql.Literal(manifest['dimension'])))
        conn.execute('ANALYZE mnemotrace.embeddings')
    return time.perf_counter() - start


def _index_used(plan):
    return plan.get('Index Name') == INDEX or any(_index_used(child) for child in plan.get('Plans', ()))


def _measure(runtime, executor, context, query, bucket, warmups, repeats, concurrency, ks):
    dao = PostgresDAO(runtime, context)
    settings = ('SET LOCAL hnsw.iterative_scan = strict_order', 'SET LOCAL enable_sort = off',
                'SET LOCAL plan_cache_mode = force_custom_plan')

    def retrieve(exact, *, explain=False):
        start = time.perf_counter()
        with dao.read_snapshot() as view:
            if not exact:
                for statement in settings:
                    view._sql.run(statement)
            corpus = Corpus(_WorkloadView(view, bucket), replace(query, exact=exact), FACT)
            if explain:
                return corpus.explain_vector()
            result = tuple(corpus.vector_scores())
        return result, (time.perf_counter() - start) * 1000

    exact_plan, approximate_plan = (retrieve(exact, explain=True) for exact in (True, False))
    reference = retrieve(True)[0]
    modes = {}
    for exact in (True, False):
        tuple(executor.map(retrieve, [exact] * warmups))
        samples = list(executor.map(retrieve, [exact] * repeats))
        ids, elapsed = zip(*samples)
        modes['exact' if exact else 'hnsw'] = dict(
            ids=ids[0], returned_count=[len(sample) for sample in ids],
            recall_at_k={k: [len(set(reference[:k]).intersection(sample[:k])) / len(reference[:k])
                             if reference else None for sample in ids] for k in ks},
            latency_ms=elapsed, p50_ms=harness._percentile(elapsed, 50), p95_ms=harness._percentile(elapsed, 95),
        )
    return dict(space=context.space_id, query=query.text, time_filter=asdict(query.facts), filter_bucket=bucket,
                concurrency=concurrency, exact_ids=reference, modes=modes, exact_plan=exact_plan,
                hnsw_plan=approximate_plan, hnsw_index_used=_index_used(approximate_plan['Plan']))


def performance(factory, out, manifest, *, warmups, repeats, case_names, concurrency_levels):
    rng = random.Random(manifest['query_vector_seed'])
    dimension = manifest['dimension']
    queries = [(text, tuple(rng.uniform(-1, 1) for _ in range(dimension))) for text in manifest['queries']]
    views = dict(current=(FactState.CURRENT, harness.BASE + DAY + 1),
                 **{'before-invalidation': (FactState.CURRENT, harness.BASE + DAY / 2),
                    'all-history': (FactState.ALL, harness.BASE + DAY + 1)})
    for case in manifest['cases']:
        name = case['name']
        if case_names and name not in case_names:
            continue
        with factory() as bundle:
            build = _load_workload(bundle, manifest, case)
            # Open and verify the restricted runtime before adding an experimental index.
            workers = max(concurrency_levels)
            with Database(replace(bundle.runtime, pool_min=workers, pool_max=workers)) as runtime:
                index_time = _create_hnsw(bundle, manifest)
                used = points = 0
                with gzip.open(out / f'{name}.jsonl.gz', 'wt', encoding='utf-8') as log:
                    header = dict(type='build', case=name, load_seconds=build, hnsw_build_seconds=index_time,
                                  embedding='seeded vectors', hnsw_iterative_scan='strict_order',
                                  enable_sort=False, plan_cache_mode='force_custom_plan')
                    log.write(json.dumps(header) + '\n')
                    spaces = [i for i in manifest['query_space_indices'] if i < case['spaces']]
                    for concurrency in concurrency_levels:
                        with ThreadPoolExecutor(max_workers=concurrency) as executor:
                            for space, (text, vector), bucket, time_name, limit in product(
                                spaces, queries, manifest['filter_buckets'], manifest['time_views'],
                                manifest['candidate_limits'],
                            ):
                                context = SpaceContext(f'user-{space % 10}', f'space-{space}')
                                state, at = views[time_name]
                                query = RetrievalQuery(FactQuery(state=state, valid_at=at, limit=limit),
                                                       text, vector, manifest['model'])
                                row = _measure(runtime, executor, context, query, bucket, warmups,
                                               repeats, concurrency, manifest['top_k'])
                                row.update(type='sample', case=name, time_view=time_name)
                                used += row['hnsw_index_used']
                                points += 1
                                log.write(json.dumps(row) + '\n')
                                if points % 25 == 0:
                                    log.flush()
                            log.flush()
                print(f'{name}: HNSW used in {used}/{points} plans', flush=True)


def main(argv=None):
    from tests.postgres.conftest import disposable_database_factory

    manifest = workload_manifest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mode', choices=('quality', 'performance', 'both'), default='both')
    parser.add_argument('--processing', choices=('immediate', 'buffered'), default='immediate',
                        help='Ingestion mode for the fixed-response quality replay')
    parser.add_argument('--case', action='append', choices=[case['name'] for case in manifest['cases']])
    parser.add_argument('--concurrency', action='append', type=int, choices=manifest['concurrency'],
                        help='Run only the selected concurrency level, repeat to select more than one')
    parser.add_argument('--warmups', type=int, default=manifest['warmup_per_query'])
    parser.add_argument('--repeats', type=int, default=manifest['measured_repeats_per_query'])
    args = parser.parse_args(argv)
    concurrency_levels = tuple(dict.fromkeys(args.concurrency or manifest['concurrency']))
    dsn = os.environ.get('MNEMOTRACE_TEST_POSTGRES_DSN')
    if not dsn or args.repeats < 1 or args.warmups < 0:
        parser.error('Set the disposable test DBA DSN and use nonnegative warmups and positive repeats')
    if args.mode != 'quality' and args.repeats < max(concurrency_levels):
        parser.error('Measured repeats must be at least the highest selected concurrency')
    metadata = dict(commit=_git('rev-parse', 'HEAD'), platform=platform.platform(), python=platform.python_version(),
                    processing=args.processing,
                    command=['python', '-m', 'eval.storage_postgres', '--out', str(args.out), '--mode', args.mode,
                             '--processing', args.processing,
                             '--warmups', str(args.warmups), '--repeats', str(args.repeats),
                             *(part for case in (args.case or ()) for part in ('--case', case)),
                             *(part for level in concurrency_levels for part in ('--concurrency', str(level)))],
                    workloads=manifest, scope='synthetic only; no real answerer or judge')
    psycopg, _ = driver()
    with psycopg.connect(dsn) as conn:
        metadata['server'] = conn.execute('SELECT version()').fetchone()[0]
        metadata['pgvector'] = conn.execute("SELECT default_version FROM pg_available_extensions WHERE name='vector'").fetchone()[0]
        metadata['parallel_workers_per_gather'] = conn.execute('SHOW max_parallel_workers_per_gather').fetchone()[0]
    diff, sources = capture_sources()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.out / 'tracked.diff').write_bytes(diff)
    (args.out / 'untracked.json').write_text(json.dumps(sources, ensure_ascii=False), encoding='utf-8')
    factory = disposable_database_factory(dsn)
    if args.mode in ('quality', 'both'):
        quality(factory, args.out, processing=args.processing)
    if args.mode in ('performance', 'both'):
        performance(factory, args.out, manifest, warmups=args.warmups, repeats=args.repeats, case_names=args.case,
                    concurrency_levels=concurrency_levels)


if __name__ == '__main__':
    main()
