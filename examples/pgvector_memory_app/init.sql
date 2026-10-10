-- Schema for the decay-engine pgvector demo.
--
-- Runs automatically on the database's first start, via Postgres'
-- /docker-entrypoint-initdb.d hook. The app re-creates nothing at runtime.
--
-- Two things here are worth noticing:
--
--   * The timing signals decay reads are first-class columns, not JSONB keys.
--     Two of them are deliberately NOT named what the library would guess
--     (last_retrieved_at, retrieval_count), so the demo has to configure a
--     FieldMap -- which is the point.
--   * created_at and last_retrieved_at are nullable. A row with neither is a
--     record decay cannot measure, and the library reports it as SKIP rather
--     than deleting it. The demo seeds one such row on purpose.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id                text        PRIMARY KEY,
    document          text        NOT NULL,
    embedding         vector(64)  NOT NULL,

    -- Written and maintained by the application, never by decay-engine.
    created_at        timestamptz,
    last_retrieved_at timestamptz,
    retrieval_count   integer     NOT NULL DEFAULT 0,
    pinned            boolean     NOT NULL DEFAULT false,

    -- Written by decay-engine when a sweep is applied: decay_cold,
    -- decay_strength, decay_swept_at. The app reads them at query time.
    metadata          jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- Cosine similarity search. Overkill for eight rows, but it is what a real
-- deployment needs and costs nothing to show.
CREATE INDEX IF NOT EXISTS memories_embedding_idx
    ON memories USING hnsw (embedding vector_cosine_ops);

-- Supports the cold-memory filter the app applies at retrieval time, and any
-- scoped sweep that filters on JSONB containment.
CREATE INDEX IF NOT EXISTS memories_metadata_idx
    ON memories USING gin (metadata);
