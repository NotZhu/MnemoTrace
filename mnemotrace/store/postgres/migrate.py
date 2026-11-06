"""Ordered transactional migrations and catalog verification, without auto-repair."""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import math

from .database import DatabaseConfig, database_errors, driver
from .errors import DatabaseConfigurationError, SchemaMismatch

MIGRATIONS = ('001_initial.sql', '002_security.sql', '003_record_commits.sql',
              '004_processing.sql', '005_retrieval.sql')


def _roles(prefix):
    return {key: prefix + '_' + suffix for key, suffix in (
        ('migration', 'migration'), ('runtime', 'runtime'),
        ('admin', 'account_admin'), ('auth', 'auth_lookup'),
        ('discovery', 'worker_discovery'), ('discovery_owner', 'worker_discovery_owner'))}


def bootstrap_roles(config: DatabaseConfig) -> None:
    """DBA-only provisioning; login credentials are managed outside the application.

    Existing role attributes are verified, not silently repaired. The migration
    role receives the documented auth-role membership needed to assign function ownership.
    """
    psycopg, _ = driver()
    from psycopg import sql
    roles = _roles(config.role_prefix)
    with database_errors(), psycopg.connect(config.dsn) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                     ('mnemotrace:roles:' + config.role_prefix,))
        for name in roles.values():
            existing = conn.execute(
                'SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb '
                'FROM pg_roles WHERE rolname = %s', (name,),
            ).fetchone()
            if existing is None:
                conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS '
                                     'NOCREATEROLE NOCREATEDB').format(sql.Identifier(name)))
            elif any(existing):
                raise DatabaseConfigurationError('Existing database role has unsafe attributes')
        conn.execute(sql.SQL('GRANT {} TO {}').format(
            sql.Identifier(roles['auth']), sql.Identifier(roles['migration'])))
        conn.execute(sql.SQL('GRANT {} TO {}').format(
            sql.Identifier(roles['discovery_owner']), sql.Identifier(roles['migration'])))
        conn.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(
            sql.Identifier(conn.info.dbname), sql.Identifier(roles['migration'])))
        conn.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        installed = conn.execute("SELECT e.extversion,n.nspname FROM pg_extension e "
                                 "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'").fetchone()
        if conn.info.server_version // 10000 != 18 or installed != ('0.8.7', 'public'):
            raise SchemaMismatch('Bootstrap requires Postgres 18 and public.vector 0.8.7')
        conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}, {}').format(
            sql.Identifier(roles['migration']), sql.Identifier(roles['runtime'])))


def _scripts(prefix):
    psycopg, _ = driver()
    scripts = []
    for name in MIGRATIONS:
        try:
            text = files(__package__).joinpath('sql', name).read_text(encoding='utf-8')
        except (OSError, UnicodeError) as exc:
            raise SchemaMismatch('Installed database migration resources are unreadable') from exc
        for marker, role in _roles(prefix).items():
            text = text.replace('{' + marker + '}', psycopg.sql.Identifier(role).as_string())
        scripts.append((text, hashlib.sha256(text.encode()).hexdigest()))
    return scripts


def structure_hash(conn) -> str:
    """Fingerprint types, columns, constraints, indexes, policies, functions and ACLs.

    Names and deparsed definitions are stable across connections; OIDs are not.
    Application data is excluded. This detects drift, not a malicious DBA.
    """
    queries = (
        "SELECT c.relname, c.relkind::text, c.relrowsecurity, c.relforcerowsecurity, "
        "pg_get_userbyid(c.relowner), c.relacl::text FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname='mnemotrace' AND c.relkind IN ('r','v','S') ORDER BY 1",
        "SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, a.attacl::text, "
        "a.attidentity::text, a.attgenerated::text, "
        "pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=c.oid "
        "AND d.adnum=a.attnum WHERE n.nspname='mnemotrace' AND c.relkind='r' "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "SELECT c.relname, k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k "
        "LEFT JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=k.connamespace "
        "WHERE n.nspname='mnemotrace' ORDER BY 1,2",
        "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='mnemotrace' ORDER BY 1",
        "SELECT tablename,policyname,roles::text,cmd,qual,with_check FROM pg_policies "
        "WHERE schemaname='mnemotrace' ORDER BY 1,2",
        "SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid),"
        "pg_get_userbyid(p.proowner),p.proacl::text FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid=p.pronamespace WHERE n.nspname='mnemotrace' ORDER BY 1,2",
        "SELECT n.nspname,pg_get_userbyid(n.nspowner),n.nspacl::text FROM pg_namespace n "
        "WHERE n.nspname='mnemotrace'",
        "SELECT t.typname,t.typtype::text,format_type(t.typbasetype,t.typtypmod) FROM pg_type t "
        "JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='mnemotrace' "
        "AND t.typtype='d' ORDER BY 1",
    )
    # Deparsing types/defaults depends on search_path. A caller's startup options
    # must not make an unchanged schema appear different. Roll back the local
    # setting along with this read-only savepoint, preserving the caller's path.
    with conn.transaction(force_rollback=True):
        conn.execute('SET LOCAL search_path = pg_catalog, public')
        catalog = [conn.execute(query).fetchall() for query in queries]
    return hashlib.sha256(json.dumps(catalog, ensure_ascii=False).encode()).hexdigest()


def check_role(conn, prefix, purpose):
    identities = conn.execute('SELECT current_user,session_user').fetchone()
    if identities != (conn.info.user, conn.info.user):
        # libpq retains the authenticated startup user even if connection options
        # change role/session authorization. RESET ROLE must never restore an admin.
        raise DatabaseConfigurationError('Database login must not override its session identity')
    role = _roles(prefix)[purpose]
    row = conn.execute(
        'SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, '
        "pg_has_role(current_user, %s, 'MEMBER'), "
        "pg_has_role(current_user, %s, 'MEMBER'), pg_has_role(current_user, %s, 'MEMBER') "
        'FROM pg_roles r WHERE rolname = current_user',
        (role, _roles(prefix)['migration'], _roles(prefix)['auth']),
    ).fetchone()
    if row is None or any(row[:4]) or not row[4] or any(row[5:]):
        raise DatabaseConfigurationError('Database credentials have unexpected privileges')
    roles = _roles(prefix)
    separated = ('runtime', 'admin') if purpose == 'discovery' else ('discovery',)
    for key in (*separated, 'discovery_owner'):
        if conn.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (roles[key],)).fetchone()[0]:
            raise DatabaseConfigurationError('Worker discovery credentials must be separate')
    other = _roles(prefix)['admin' if purpose == 'runtime' else 'runtime']
    if conn.execute("SELECT pg_has_role(current_user, %s, 'MEMBER')", (other,)).fetchone()[0]:
        raise DatabaseConfigurationError('Runtime and administration credentials must be separate')
    if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE "
                    "pg_has_role(current_user,oid,'MEMBER') AND "
                    '(rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb))').fetchone()[0]:
        raise DatabaseConfigurationError('Database credentials can assume an elevated role')


def check_schema(conn, prefix, *, require_current=True):
    scripts = _scripts(prefix)
    try:
        rows = conn.execute(
            'SELECT version,checksum,role_prefix,structure_hash FROM '
            'mnemotrace.schema_migrations ORDER BY version').fetchall()
        extension = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        if (conn.info.server_version // 10000 != 18 or extension != ('0.8.7',)
                or not 1 <= len(rows) <= len(scripts)
                or require_current and len(rows) != len(scripts)):
            raise SchemaMismatch('Unsupported database, extension or schema version')
        if any(row[:3] != (i, checksum, prefix)
               for i, (row, (_, checksum)) in enumerate(zip(rows, scripts), 1)):
            raise SchemaMismatch('Schema migration history is incompatible')
        if rows[-1][3] != structure_hash(conn):
            raise SchemaMismatch('Database structure differs from the applied migrations')
        return len(rows)
    except (driver()[0].OperationalError, driver()[0].InterfaceError):
        raise
    except driver()[0].Error as exc:
        raise SchemaMismatch('Required database structure is missing or unreadable') from exc


def migrate(config: DatabaseConfig) -> None:
    psycopg, _ = driver()
    from psycopg import sql
    with database_errors(), psycopg.connect(
            config.dsn, connect_timeout=math.ceil(config.connect_timeout)) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                     ('mnemotrace:schema',))
        # A privileged login must intentionally enter the migration role; runtime
        # credentials cannot use this path to obtain DDL privileges.
        conn.execute(sql.SQL('SET LOCAL ROLE {}').format(
            sql.Identifier(_roles(config.role_prefix)['migration'])))
        existing = conn.execute("SELECT to_regnamespace('mnemotrace')").fetchone()[0]
        applied = check_schema(conn, config.role_prefix, require_current=False) if existing is not None else 0
        for version, (text, checksum) in enumerate(_scripts(config.role_prefix), 1):
            if version <= applied:
                continue
            conn.execute(text, prepare=False)
            conn.execute(
                'INSERT INTO mnemotrace.schema_migrations(version,checksum,role_prefix,structure_hash) '
                'VALUES (%s,%s,%s,%s)', (version, checksum, config.role_prefix, structure_hash(conn)))
        check_schema(conn, config.role_prefix)
