CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE mnemotrace_next.spaces (
    space_id text COLLATE "C" PRIMARY KEY,
    owner_id text COLLATE "C" NOT NULL,
    name text NOT NULL,
    created_at double precision NOT NULL,
    purge_generation bigint NOT NULL DEFAULT 0
);

CREATE TABLE mnemotrace_next.messages (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    message_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    append_seq bigint GENERATED ALWAYS AS IDENTITY,
    role text NOT NULL,
    content text NOT NULL,
    event_time double precision,
    recorded_at double precision NOT NULL,
    PRIMARY KEY (space_id, message_id)
);

CREATE INDEX messages_session_tail
    ON mnemotrace_next.messages (space_id, session_id, append_seq DESC);

CREATE TABLE mnemotrace_next.entities (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    entity_id text COLLATE "C" NOT NULL,
    name_key text COLLATE "C" NOT NULL,
    name text NOT NULL,
    type text,
    PRIMARY KEY (space_id, entity_id),
    UNIQUE (space_id, name_key)
);

CREATE TABLE mnemotrace_next.facts (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    subject_id text COLLATE "C" NOT NULL,
    predicate text COLLATE "C" NOT NULL,
    object_entity_id text COLLATE "C",
    object_value text,
    object_kind text,
    text text NOT NULL,
    valid_at double precision,
    invalid_at double precision,
    recorded_at double precision NOT NULL,
    expired_at double precision,
    labels text[] NOT NULL,
    sensitive boolean NOT NULL,
    source text NOT NULL,
    lexemes tsvector NOT NULL,
    PRIMARY KEY (space_id, fact_id),
    FOREIGN KEY (space_id, subject_id) REFERENCES mnemotrace_next.entities,
    FOREIGN KEY (space_id, object_entity_id) REFERENCES mnemotrace_next.entities,
    CHECK (num_nonnulls(object_entity_id, object_value) = 1),
    CHECK (object_value IS NOT NULL OR object_kind IS NULL),
    CHECK ((invalid_at IS NULL) = (expired_at IS NULL)),
    CHECK (valid_at < invalid_at),
    CHECK (recorded_at <= expired_at)
);

CREATE INDEX facts_timeline
    ON mnemotrace_next.facts (space_id, recorded_at DESC, fact_id);
CREATE INDEX facts_slot
    ON mnemotrace_next.facts (space_id, subject_id, predicate, recorded_at DESC, fact_id);
CREATE INDEX facts_cutoff
    ON mnemotrace_next.facts (space_id, invalid_at) WHERE invalid_at IS NOT NULL;
CREATE INDEX facts_edges_out
    ON mnemotrace_next.facts (space_id, subject_id, valid_at DESC NULLS LAST, fact_id)
    WHERE object_entity_id IS NOT NULL;
CREATE INDEX facts_edges_in
    ON mnemotrace_next.facts (space_id, object_entity_id, valid_at DESC NULLS LAST, fact_id)
    WHERE object_entity_id IS NOT NULL;
CREATE INDEX facts_labels ON mnemotrace_next.facts USING gin (labels);
CREATE INDEX facts_lexemes ON mnemotrace_next.facts USING gin (lexemes);

CREATE TABLE mnemotrace_next.fact_provenance (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    position integer NOT NULL,
    message_id text COLLATE "C" NOT NULL,
    PRIMARY KEY (space_id, fact_id, position),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE,
    FOREIGN KEY (space_id, message_id) REFERENCES mnemotrace_next.messages
);

CREATE INDEX fact_provenance_message
    ON mnemotrace_next.fact_provenance (space_id, message_id, fact_id);

CREATE TABLE mnemotrace_next.fact_supersedes (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    position integer NOT NULL,
    predecessor_id text COLLATE "C" NOT NULL,
    PRIMARY KEY (space_id, fact_id, position),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE,
    FOREIGN KEY (space_id, predecessor_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE
);

CREATE INDEX fact_supersedes_predecessor
    ON mnemotrace_next.fact_supersedes (space_id, predecessor_id);

CREATE TABLE mnemotrace_next.fact_changes (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    cursor bigint GENERATED ALWAYS AS IDENTITY,
    changed_at double precision NOT NULL,
    before jsonb,
    after jsonb NOT NULL,
    source_message_ids text[] NOT NULL,
    PRIMARY KEY (space_id, fact_id, cursor),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE
);

CREATE INDEX fact_changes_sources
    ON mnemotrace_next.fact_changes USING gin (source_message_ids);

CREATE TABLE mnemotrace_next.digests (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    digest_id text COLLATE "C" NOT NULL,
    session_id text COLLATE "C" NOT NULL,
    text text NOT NULL,
    source_message_ids text[] NOT NULL CHECK (cardinality(source_message_ids) > 0),
    last_message_seq bigint NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    PRIMARY KEY (space_id, digest_id)
);

CREATE INDEX digests_latest ON mnemotrace_next.digests
    (space_id, session_id, last_message_seq DESC, updated_at DESC, digest_id);

CREATE TABLE mnemotrace_next.profiles (
    space_id text COLLATE "C" PRIMARY KEY REFERENCES mnemotrace_next.spaces,
    data jsonb NOT NULL,
    updated_at double precision NOT NULL
);

CREATE TABLE mnemotrace_next.fact_salience (
    space_id text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    salience double precision NOT NULL CHECK (salience BETWEEN 1.0 AND 3.0),
    last_access double precision,
    PRIMARY KEY (space_id, fact_id),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE
);

CREATE TABLE mnemotrace_next.embedding_spaces (
    space_id text COLLATE "C" REFERENCES mnemotrace_next.spaces NOT NULL,
    embedding_space text COLLATE "C" NOT NULL,
    dim integer NOT NULL CHECK (dim > 0),
    PRIMARY KEY (space_id, embedding_space),
    UNIQUE (space_id, embedding_space, dim)
);

CREATE TABLE mnemotrace_next.fact_vectors (
    space_id text COLLATE "C" NOT NULL,
    embedding_space text COLLATE "C" NOT NULL,
    fact_id text COLLATE "C" NOT NULL,
    dim integer NOT NULL,
    embedding public.vector NOT NULL,
    PRIMARY KEY (space_id, embedding_space, fact_id),
    FOREIGN KEY (space_id, fact_id) REFERENCES mnemotrace_next.facts ON DELETE CASCADE,
    FOREIGN KEY (space_id, embedding_space, dim)
        REFERENCES mnemotrace_next.embedding_spaces (space_id, embedding_space, dim),
    CHECK (public.vector_dims(embedding) = dim),
    CHECK (public.vector_norm(embedding) > 0)
);

CREATE INDEX fact_vectors_fact ON mnemotrace_next.fact_vectors (space_id, fact_id);
