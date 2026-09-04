-- Enable the extensions CivicOS uses on PostgreSQL.
-- pgvector powers document retrieval; without it the platform automatically
-- falls back to in-process cosine scoring, so this is an optimisation, not a
-- requirement.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
