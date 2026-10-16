-- The memory is one Postgres (ADR 0006): TimescaleDB partitions it by day, and pgvector
-- holds and searches the appearance embeddings.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
