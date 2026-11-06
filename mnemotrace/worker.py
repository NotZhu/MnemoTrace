"""Run the Postgres processing worker with separate discovery credentials."""
from argparse import ArgumentParser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import os
import time

from ._postgres_pipeline import PostgresPipeline
from .config import Config
from .llm.providers import load_dotenv, make_embedder, make_llm
from .store.postgres.dao import PostgresDAO
from .store.postgres.database import Database, DatabaseConfig
from .store.postgres.errors import AccountNotFound, DatabaseError, DatabaseUnavailable, SpaceNotFound

logger = logging.getLogger(__name__)


def run_worker(discovery, runtime, config, llm, embedder, model, *, concurrency=2, once=False):
    def run(candidate):
        context, operation_id = candidate
        try:
            return PostgresPipeline(PostgresDAO(runtime, context), config, llm, embedder, model).run_one(operation_id)
        except (AccountNotFound, SpaceNotFound):
            return False

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        running = {}
        while True:
            try:
                for candidate, future in tuple(running.items()):
                    if future.done():
                        del running[candidate]
                        future.result()
                if len(running) < concurrency:
                    for candidate in discovery.discover_processing(100):
                        if candidate not in running:
                            running[candidate] = executor.submit(run, candidate)
                            if len(running) == concurrency:
                                break
                if once:
                    for future in running.values():
                        future.result()
                    return
                if running:
                    wait(running.values(), timeout=1, return_when=FIRST_COMPLETED)
                    continue
            except DatabaseUnavailable:
                if once:
                    raise
                logger.warning('Postgres processing is temporarily unavailable')
            time.sleep(1)


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--once', action='store_true', help='Execute at most one stage per available slot')
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 32:
        parser.error('concurrency must be between 1 and 32')
    load_dotenv()
    name = os.environ.get('MNEMOTRACE_LLM', '').strip()
    if not name:
        parser.error('Set MNEMOTRACE_LLM to enable automatic processing')
    embedder_name = os.environ.get('MNEMOTRACE_EMBEDDER', 'hashing')
    model = 'hashing-v1' if embedder_name == 'hashing' else embedder_name
    try:
        llm, embedder = make_llm(name), make_embedder(embedder_name)
        config = Config()
        config.llm_topics = os.environ.get('MNEMOTRACE_LLM_TOPICS') != '0'
        with (Database(DatabaseConfig.from_env()) as runtime,
              Database(DatabaseConfig.from_env('discovery'), purpose='discovery') as discovery):
            run_worker(discovery, runtime, config, llm, embedder, model,
                       concurrency=args.concurrency, once=args.once)
    except KeyboardInterrupt:
        return 0
    except (DatabaseError, ImportError, ValueError):
        parser.exit(1, 'Worker configuration or database access failed. Check credentials, schema and model settings.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
