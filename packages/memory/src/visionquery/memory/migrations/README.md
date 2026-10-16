# Migrations

The memory's schema, applied by `visionquery-migrate` (ADR 0011, `visionquery.memory.migrate`).

- A change to the schema is a new file, numbered one after the last: `NNNN_lowercase_words.sql`.
- A file that has been applied anywhere is never edited, renamed, renumbered or removed; the runner refuses to run when one is.
- Each file runs in its own transaction, so it must not contain `BEGIN`, `COMMIT` or statements that cannot run in a transaction (such as `CREATE INDEX CONCURRENTLY`).
- A file runs as written, with no parameters, so `%` needs no escaping.
