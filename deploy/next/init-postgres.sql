\getenv migration_password MNEMOTRACE_NEXT_MIGRATION_PASSWORD
\getenv runtime_password MNEMOTRACE_NEXT_RUNTIME_PASSWORD
\getenv admin_password MNEMOTRACE_NEXT_ADMIN_PASSWORD

BEGIN;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE ROLE mnemotrace_next_migrate LOGIN PASSWORD :'migration_password';
CREATE ROLE mnemotrace_next_runtime LOGIN PASSWORD :'runtime_password';
CREATE ROLE mnemotrace_next_admin LOGIN PASSWORD :'admin_password';

REVOKE CONNECT, TEMPORARY ON DATABASE mnemotrace_next FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE mnemotrace_next TO mnemotrace_next_migrate;
GRANT CONNECT ON DATABASE mnemotrace_next
    TO mnemotrace_next_runtime, mnemotrace_next_admin;
COMMIT;
