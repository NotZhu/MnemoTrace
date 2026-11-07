#!/usr/bin/env python3
"""Exercise the production Compose stack with a disposable project and synthetic data."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def main():
    project = 'mnemotrace-smoke-' + secrets.token_hex(6)
    image = 'mnemotrace-memory:' + project
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    with TemporaryDirectory(prefix=project) as directory:
        env_file = Path(directory) / '.env'
        password = secrets.token_urlsafe(24)
        urls = {
            name: f'postgresql://{login}:{secret}@db:5432/mnemotrace'
            for name, login, secret in (
                ('BOOTSTRAP_DATABASE_URL', 'postgres', password),
                ('DATABASE_URL', 'app_runtime', secrets.token_urlsafe(24)),
                ('MIGRATION_DATABASE_URL', 'app_migration', secrets.token_urlsafe(24)),
                ('ADMIN_DATABASE_URL', 'app_account_admin', secrets.token_urlsafe(24)),
                ('DISCOVERY_DATABASE_URL', 'app_worker_discovery', secrets.token_urlsafe(24)),
            )
        }
        variables = dict(urls, POSTGRES_PASSWORD=password, PORT=str(port), IMAGE=image, EMBEDDER='hashing',
                         LLM='', ANSWERER='')
        env_file.write_text(''.join(f'MNEMOTRACE_{name}={value}\n'
                                    for name, value in variables.items()), encoding='utf-8')
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(('MNEMOTRACE_', 'COMPOSE_'))}
        env['DEEPSEEK_API_KEY'] = ''
        command = ['docker', 'compose', '-p', project, '--env-file', str(env_file),
                   '-f', 'deploy/docker-compose.yml']

        def compose(*args, capture=False, expected=0, extra=None):
            result = subprocess.run([*command, *args], cwd=ROOT, env={**env, **(extra or {})},
                                    stdout=subprocess.PIPE if capture else None, text=True, timeout=900)
            if result.returncode != expected:
                raise RuntimeError(f'Compose {args[0]} exited with status {result.returncode}')
            return json.loads(result.stdout) if capture else None

        def http(path, body=None, *, key=None, method=None, expected=200):
            headers = {'Content-Type': 'application/json'}
            if key:
                headers['Authorization'] = 'Bearer ' + key
            req = Request(f'http://127.0.0.1:{port}{path}', headers=headers,
                          data=json.dumps(body).encode() if body is not None else None, method=method)
            try:
                response = urlopen(req, timeout=15)
            except HTTPError as exc:
                response = exc
            with response:
                assert response.status == expected, (path, response.status, expected)
                return json.load(response)

        built = False
        try:
            compose('up', '-d', '--wait', 'db')
            compose('build', 'mnemotrace')
            built = True
            compose('run', '--rm', 'db-init', 'python', '-m', 'mnemotrace.admin',
                    'init-db', '--bootstrap', '--provision-logins', '--roles-only')
            compose('run', '--rm', 'mnemotrace', 'python', '-m', 'mnemotrace.admin',
                    'check-db', expected=1)
            for _ in range(2):
                assert compose('run', '--rm', 'db-init', capture=True)['initialized']
            compose('run', '--rm', 'mnemotrace', 'python', '-m', 'mnemotrace.admin',
                    'check-db', expected=1,
                    extra={'MNEMOTRACE_DATABASE_URL': urls['ADMIN_DATABASE_URL']})
            compose('up', '-d', '--wait', 'mnemotrace', 'worker')
            user = compose('run', '--rm', 'admin', 'create-user', capture=True)['user_id']
            key = compose('run', '--rm', 'admin', 'issue-key', '--user-id', user,
                          capture=True)['key']
            space = http('/v1/spaces', {'name': 'Synthetic deployment'}, key=key,
                         expected=201)['space_id']
            query = '?space_id=' + space
            message = {'space_id': space, 'session_id': 'smoke', 'processing': 'buffered',
                       'messages': [{'message_id': 'once', 'role': 'user', 'content': 'Synthetic archive note'}]}
            rejection = http('/v1/messages', message, key=key, expected=503)
            assert rejection['detail']['code'] == 'llm_not_configured'
            assert not http('/v1/memories' + query, key=key)['episodes']
            compose('stop', 'worker')
            raw = http('/v1/messages', {**message, 'scope': 'raw'}, key=key, expected=202)
            source_id, = raw['source_ids']
            assert raw['accepted'] == 1
            http('/v1/facts', {'space_id': space, 'predicate': 'lives_in', 'object': 'Synthetic City'},
                 key=key)
            processing = http('/v1/processing' + query, key=key)
            assert processing['operations']
            vector = next(item['operation_id'] for item in processing['operations']
                          if item['kind'] == 'reembed')
            assert http('/v1/operations/' + vector + query, key=key)['state'] == 'pending'
            compose('stop', 'mnemotrace', 'worker')
            compose('restart', 'db')
            compose('up', '-d', '--wait', 'mnemotrace', 'worker')
            for _ in range(30):
                operation = http('/v1/operations/' + vector + query, key=key)
                if operation['state'] == 'completed':
                    break
                time.sleep(1)
            assert operation['state'] == 'completed'
            assert 'Synthetic archive note' in json.dumps(http('/v1/episodes/' + source_id + query, key=key))
            recalled = http('/v1/recall', {'space_id': space, 'query': 'Where do I live?'}, key=key)
            assert 'Synthetic City' in json.dumps(recalled)
            assert http(f'/v1/spaces/{space}?confirm=true', key=key, method='DELETE')['ok']
            assert not http('/v1/spaces', key=key)['spaces']
            compose('stop', 'db')
            try:
                assert not http('/ready', expected=503)['ready']
            except URLError as exc:
                raise RuntimeError('Running API did not return a readiness failure') from exc
        finally:
            compose('down', '--volumes', '--remove-orphans')
            if built:
                subprocess.run(['docker', 'image', 'rm', image], cwd=ROOT, env=env,
                               check=True, timeout=60)
    print('OK isolated Compose deployment verification passed')


if __name__ == '__main__':
    main()
