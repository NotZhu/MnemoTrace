import json
import os
import subprocess
import sys


def test_admin_cli_full_lifecycle(cluster, spaces):
    env = {**os.environ,
           'MNEMOTRACE_DB_ROLE_PREFIX': cluster.runtime.role_prefix,
           'MNEMOTRACE_MIGRATION_DATABASE_URL': cluster.migration.dsn,
           'MNEMOTRACE_ADMIN_DATABASE_URL': cluster.admin.dsn,
           'MNEMOTRACE_BOOTSTRAP_DATABASE_URL': cluster.bootstrap.dsn}
    def run(*args, success=True):
        result = subprocess.run([sys.executable, '-m', 'mnemotrace.admin', *args],
                                env=env, capture_output=True, text=True, timeout=30)
        assert (result.returncode == 0) == success, result.stderr
        assert all(config.dsn not in result.stdout + result.stderr
                   for config in (cluster.migration, cluster.admin, cluster.bootstrap))
        return json.loads(result.stdout if success else result.stderr)
    assert run('init-db', '--bootstrap') == {'initialized': True}
    user = run('create-user')['user_id']
    try:
        issued = run('issue-key', '--user-id', user, '--name', 'synthetic')
        assert spaces.authenticate(issued['key']).user_id == user
        listing = run('list-keys', '--user-id', user)['keys']
        assert len(listing) == 1
        assert 'key' not in listing[0] and 'key_hash' not in listing[0]
        replacement = run('rotate-key', '--user-id', user, '--key-id', issued['key_id'])
        assert spaces.authenticate(replacement['key']).user_id == user
        assert run('revoke-key', '--user-id', user, '--key-id', replacement['key_id']) == {'revoked': True}
    finally:
        assert run('delete-user', '--user-id', user) == {'deleted': True}
    assert run('issue-key', '--user-id', user, success=False)['error'] == 'account_not_found'


def test_cli_invalid_configuration_does_not_expose_credentials(cluster):
    secret = 'synthetic-not-a-real-password'
    result = subprocess.run([sys.executable, '-m', 'mnemotrace.admin', 'create-user'],
                            env={**os.environ, 'MNEMOTRACE_ADMIN_DATABASE_URL':
                                 'invalid=' + secret},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error'] == 'database_configuration'
    assert secret not in result.stdout + result.stderr
