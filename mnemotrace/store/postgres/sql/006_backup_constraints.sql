-- BETWEEN combined with AND can deparse with nested AND nodes that pg_dump
-- flattens on reload. Use explicit bounds so the structure hash round-trips.
ALTER DOMAIN mnemotrace.identifier
    DROP CONSTRAINT identifier_check;
ALTER DOMAIN mnemotrace.identifier
    ADD CONSTRAINT identifier_check
    CHECK (length(VALUE) >= 1 AND length(VALUE) <= 512 AND VALUE ~ '[^[:space:]]');

ALTER TABLE mnemotrace.memory_spaces
    DROP CONSTRAINT memory_spaces_name_check,
    ADD CONSTRAINT memory_spaces_name_check
    CHECK (length(name) >= 1 AND length(name) <= 128 AND name ~ '[^[:space:]]');
