# Checksummed append-only SQL migrations instead of Alembic

Schema changes are numbered raw SQL files, applied in order under an advisory lock, each recorded with its checksum; an edited or reordered file is refused.
Alembic was rejected because TimescaleDB and pgvector DDL (hypertables, per-partition vector indexes) is expressed naturally in SQL and poorly through ORM autogeneration.
