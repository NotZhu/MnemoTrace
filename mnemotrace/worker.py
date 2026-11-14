"""Run the Postgres processing worker with separate discovery credentials."""
from argparse import ArgumentParser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import os
import signal
from time import monotonic, sleep

from ._postgres_pipeline import PostgresPipeline
from .config import Config
from .embed import EmbeddingUnavailable
from .llm.providers import configured_embedder, load_dotenv, make_llm
from .store.postgres.dao import PostgresDAO
from .store.postgres.database import Database, DatabaseConfig
from .store.postgres.errors import AccountNotFound, DatabaseError, DatabaseUnavailable, SpaceNotFound

logger = logging.getLogger(__name__)
DATABASE_WARNING_INTERVAL = 60


def run_worker(discovery, runtime, config, llm, embedder, model, *, concurrency=2, once=False,
               should_stop=lambda: False):
    def run(candidate):
        context, operation_id = candidate
        try:
            return PostgresPipeline(PostgresDAO(runtime, context), config, llm, embedder, model).run_next_stage(operation_id)
        except (AccountNotFound, SpaceNotFound):
            return False

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        running = {}
        next_database_warning = float('-inf')
        while not should_stop():
            try:
                for candidate, future in tuple(running.items()):
                    if future.done():
                        del running[candidate]
                        future.result()
                if len(running) < concurrency:
                    for candidate in (discovery.discover_processing(100) if llm is not None else ()):
                        if should_stop():
                            break
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
                now = monotonic()
                if now >= next_database_warning:
                    logger.warning('Postgres processing is temporarily unavailable')
                    next_database_warning = now + DATABASE_WARNING_INTERVAL
            if not should_stop():
                sleep(1)


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--once', action='store_true', help='Execute at most one stage per available slot')
    args = parser.parse_args(argv)
    if not 1 <= args.concurrency <= 32:
        parser.error('concurrency must be between 1 and 32')
    load_dotenv()
    name = os.environ.get('MNEMOTRACE_LLM', '').strip()
    stopping = False

    def request_stop(*_):
        # A signal can interrupt the main thread while it holds a threading lock.
        nonlocal stopping
        stopping = True

    previous_handler = signal.signal(signal.SIGTERM, request_stop)
    try:
        llm = make_llm(name) if name else None
        embedder, model = configured_embedder()
        config = Config.from_env()
        with (Database(DatabaseConfig.from_env()) as runtime,
              Database(DatabaseConfig.from_env('discovery'), purpose='discovery') as discovery):
            run_worker(discovery, runtime, config, llm, embedder, model,
                       concurrency=args.concurrency, once=args.once, should_stop=lambda: stopping)
    except KeyboardInterrupt:
        return 0
    except EmbeddingUnavailable as exc:
        parser.exit(1, f"{exc}\n")
    except (DatabaseError, ImportError, ValueError):
        parser.exit(1, 'Worker configuration or database access failed. Check credentials, schema and model settings.\n')
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
