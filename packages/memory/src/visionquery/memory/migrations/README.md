# Migrations

The memory's schema, applied by `visionquery-migrate` (ADR 0011, `visionquery.memory.migrate`).

- A change to the schema is a new file, numbered one after the last: `NNNN_lowercase_words.sql`.
- A file that has been applied anywhere is never edited, renamed, renumbered or removed; the runner refuses to run when one is.
- Each file runs in its own transaction, together with the row that records it.
- A file must not contain `BEGIN`, `COMMIT`, `ROLLBACK`, `END`, `ABORT` or `START TRANSACTION` as a statement; the runner refuses a pending file that does, naming it, and applies nothing.
  Strings, comments, quoted identifiers and function bodies are not statements, so a `BEGIN ... END;` inside `$$` is fine, and so is `ROLLBACK TO SAVEPOINT`.
- A file must not contain statements that cannot run in a transaction (such as `CREATE INDEX CONCURRENTLY`); Postgres rejects them and the file is rolled back.
- A runner that is killed mid-migration keeps holding the advisory lock until its server-side statement finishes.
  Postgres then releases the lock, because its session has ended, and the next runner proceeds.
- A file runs as written, with no parameters, so `%` needs no escaping.
- What a file leaves on its session (settings, a role, temporary tables, prepared statements, held cursors, `LISTEN`s) is reset before the next file, so files behave the same applied together or one run at a time.
- `visionquery-migrate status` lists what is applied and pending, and what a run would refuse, without changing anything.
