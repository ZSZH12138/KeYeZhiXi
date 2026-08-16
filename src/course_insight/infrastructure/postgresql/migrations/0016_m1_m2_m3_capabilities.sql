-- S1-S6 PostgreSQL authority: complete artifacts, pgvector and review metadata.
-- The vector column intentionally has no fixed dimension.  The dimension is
-- bound per immutable index row and enforced by the adapter, which permits a
-- model migration without rewriting this migration or the whole table.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE m1_m2_m3_artifacts (
    module TEXT NOT NULL CHECK (module IN ('m1', 'm2', 'm3')),
    object_type TEXT NOT NULL CHECK (length(object_type) > 0),
    object_id TEXT NOT NULL CHECK (length(object_id) > 0),
    object_version TEXT NOT NULL CHECK (length(object_version) > 0),
    status TEXT NOT NULL CHECK (length(status) > 0),
    content_checksum CHAR(64) NOT NULL CHECK (
        content_checksum ~ '^[0-9a-f]{64}$'
    ),
    payload_version TEXT NOT NULL CHECK (length(payload_version) > 0),
    payload_checksum CHAR(64) NOT NULL CHECK (
        payload_checksum ~ '^[0-9a-f]{64}$'
    ),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (module, object_type, object_id, object_version),
    CHECK (
        (module = 'm1' AND object_type = 'course_import')
        OR (module = 'm2' AND object_type = 'evidence_index')
        OR (module = 'm3' AND object_type IN (
            'knowledge_bundle', 'knowledge_validation'
        ))
    )
);

CREATE INDEX m1_m2_m3_artifacts_content_lookup
ON m1_m2_m3_artifacts(module, object_type, content_checksum);

CREATE TABLE m2_vector_indexes (
    index_id TEXT NOT NULL CHECK (length(index_id) > 0),
    index_version TEXT NOT NULL CHECK (length(index_version) > 0),
    dimension INTEGER NOT NULL CHECK (dimension > 0 AND dimension <= 16000),
    embedding_model_id TEXT CHECK (
        embedding_model_id IS NULL OR length(embedding_model_id) > 0
    ),
    status TEXT NOT NULL CHECK (status IN ('staging', 'ready')),
    checksum CHAR(64) CHECK (
        checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'
    ),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    PRIMARY KEY (index_id, index_version),
    UNIQUE (index_id, index_version, dimension),
    CHECK (
        (status = 'staging' AND checksum IS NULL AND published_at IS NULL)
        OR (status = 'ready' AND checksum IS NOT NULL AND published_at IS NOT NULL)
    ),
    CHECK (
        (status = 'staging' AND chunk_count = 0)
        OR (status = 'ready' AND chunk_count > 0)
    )
);

CREATE INDEX m2_vector_indexes_ready_lookup
ON m2_vector_indexes(status, index_id, index_version);

CREATE TABLE m2_vector_documents (
    index_id TEXT NOT NULL,
    index_version TEXT NOT NULL,
    evidence_id TEXT NOT NULL CHECK (length(evidence_id) > 0),
    chunk_id TEXT NOT NULL CHECK (length(chunk_id) > 0),
    text_checksum CHAR(64) NOT NULL CHECK (
        text_checksum ~ '^[0-9a-f]{64}$'
    ),
    dimension INTEGER NOT NULL CHECK (dimension > 0 AND dimension <= 16000),
    embedding vector NOT NULL CHECK (vector_dims(embedding) = dimension),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_id, index_version, evidence_id),
    FOREIGN KEY (index_id, index_version)
        REFERENCES m2_vector_indexes(index_id, index_version)
        ON DELETE CASCADE,
    FOREIGN KEY (index_id, index_version, dimension)
        REFERENCES m2_vector_indexes(index_id, index_version, dimension)
        ON DELETE CASCADE
);

-- A composite lookup index is safe for the migration-safe variable-dimension
-- vector strategy.  A deployment may add an expression ANN index for each
-- configured model dimension without changing this authoritative schema.
CREATE INDEX m2_vector_documents_lookup
ON m2_vector_documents(index_id, index_version, evidence_id);

CREATE TABLE m2_retrieval_audits (
    audit_id TEXT PRIMARY KEY CHECK (length(audit_id) > 0),
    query_id TEXT NOT NULL CHECK (length(query_id) > 0),
    index_id TEXT NOT NULL CHECK (length(index_id) > 0),
    index_version TEXT NOT NULL CHECK (length(index_version) > 0),
    policy_id TEXT NOT NULL CHECK (length(policy_id) > 0),
    status TEXT NOT NULL CHECK (status IN ('empty', 'succeeded', 'failed')),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL CHECK (
        payload_checksum ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX m2_retrieval_audits_query_lookup
ON m2_retrieval_audits(query_id, created_at, audit_id);

CREATE INDEX m2_retrieval_audits_index_lookup
ON m2_retrieval_audits(index_id, index_version, created_at);

CREATE TABLE m3_teacher_reviews (
    review_id TEXT PRIMARY KEY CHECK (length(review_id) > 0),
    subject_id TEXT NOT NULL CHECK (length(subject_id) > 0),
    input_checksum CHAR(64) NOT NULL CHECK (
        input_checksum ~ '^[0-9a-f]{64}$'
    ),
    validation_report_ref TEXT NOT NULL CHECK (length(validation_report_ref) > 0),
    state TEXT NOT NULL CHECK (
        state IN ('draft', 'submitted', 'approved', 'rejected', 'recalled')
    ),
    version INTEGER NOT NULL CHECK (version > 0),
    reviewer_pseudonym TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    history JSONB NOT NULL CHECK (jsonb_typeof(history) = 'array'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL CHECK (
        payload_checksum ~ '^[0-9a-f]{64}$'
    ),
    CHECK (updated_at >= created_at),
    CHECK (
        reviewer_pseudonym IS NULL OR char_length(reviewer_pseudonym) <= 128
    ),
    CHECK (reason IS NULL OR char_length(reason) <= 2000)
);

CREATE INDEX m3_teacher_reviews_state_lookup
ON m3_teacher_reviews(state, updated_at, review_id);

CREATE INDEX m3_teacher_reviews_subject_lookup
ON m3_teacher_reviews(subject_id, updated_at, review_id);
