\getenv migration_password MNEMOTRACE_MIGRATION_PASSWORD
\getenv runtime_password MNEMOTRACE_RUNTIME_PASSWORD
\getenv admin_password MNEMOTRACE_ADMIN_PASSWORD

BEGIN;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE ROLE mnemotrace_migrate LOGIN PASSWORD :'migration_password';
CREATE ROLE mnemotrace_runtime LOGIN PASSWORD :'runtime_password';
CREATE ROLE mnemotrace_admin LOGIN PASSWORD :'admin_password';

REVOKE CONNECT, TEMPORARY ON DATABASE mnemotrace FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE mnemotrace TO mnemotrace_migrate;
GRANT CONNECT ON DATABASE mnemotrace
    TO mnemotrace_runtime, mnemotrace_admin;
COMMIT;
