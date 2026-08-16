-- Bind every ready vector index to the governed package and embedding model.
-- This is a new migration so an already-applied 0014 remains checksum-locked.
ALTER TABLE m2_vector_indexes
    ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata) = 'object');
