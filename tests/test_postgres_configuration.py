"""The database optional boundary must remain usable without importing drivers."""
import os
import json
import subprocess
import sys

import pytest

from mnemotrace.store.postgres import Database, DatabaseConfig
from mnemotrace.store.postgres.errors import DatabaseConfigurationError


@pytest.mark.parametrize('kwargs', [
    {'dsn': ''}, {'dsn': None}, {'role_prefix': 'x;DROP ROLE y'},
    {'pool_min': 0}, {'pool_max': 0}, {'pool_min': True},
    {'pool_min': 3, 'pool_max': 2}, {'pool_timeout': float('nan')},
    {'connect_timeout': 0}, {'open_timeout': float('inf')},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(DatabaseConfigurationError):
        DatabaseConfig(**{'dsn': 'postgresql://synthetic:do-not-display@localhost/example', **kwargs})


def test_configuration_hides_credentials(monkeypatch):
    secret = 'synthetic-password-not-a-real-credential'
    config = DatabaseConfig('postgresql://user:' + secret + '@localhost/example')
    assert secret not in repr(config)
    monkeypatch.setenv('MNEMOTRACE_DB_POOL_MIN', secret)
    with pytest.raises(DatabaseConfigurationError) as caught:
        DatabaseConfig.from_env()
    assert secret not in str(caught.value)


def test_distinct_credentials(monkeypatch):
    monkeypatch.setenv('MNEMOTRACE_DATABASE_URL', 'runtime')
    monkeypatch.setenv('MNEMOTRACE_ADMIN_DATABASE_URL', 'admin')
    monkeypatch.setenv('MNEMOTRACE_MIGRATION_DATABASE_URL', 'migration')
    assert DatabaseConfig.from_env().dsn == 'runtime'
    assert DatabaseConfig.from_env('admin').dsn == 'admin'
    assert DatabaseConfig.from_env('migration').dsn == 'migration'
    database = Database(DatabaseConfig('unused'))
    database.close()
    database.close()


def test_core_import_and_missing_database_extra():
    program = '''
import importlib.abc
import sys
class BlockDrivers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'psycopg', 'psycopg_pool'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockDrivers())
from mnemotrace import Memory
from mnemotrace.store.postgres import Database, DatabaseConfig
from mnemotrace.store.postgres.errors import DatabaseDependencyError
assert not any(name.startswith('psycopg') for name in sys.modules)
memory = Memory()
memory.add_fact('Synthetic user', 'likes', 'tea')
try:
    Database(DatabaseConfig('dbname=unused')).open()
except DatabaseDependencyError as error:
    assert 'mnemotrace-memory[postgres]' in str(error)
else:
    raise AssertionError('Missing drivers did not fail')
import os
os.environ['MNEMOTRACE_ADMIN_DATABASE_URL'] = 'dbname=unused'
from mnemotrace.admin import main
assert main(['create-user']) == 1
'''
    completed = subprocess.run([sys.executable, '-c', program], check=True,
                               capture_output=True, text=True, env=os.environ.copy())
    assert json.loads(completed.stderr)['error'] == 'database_dependency'
