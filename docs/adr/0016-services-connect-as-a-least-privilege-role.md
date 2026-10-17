# Services connect to the memory as a least-privilege role; only migrating is a superuser

Only `visionquery-migrate` connects to Postgres as the superuser that owns the memory.
Every long-running VisionQuery service connects as `visionquery_runtime`, which may connect to the memory's database and read and write the rows of its tables, and nothing more: it cannot create, alter, drop or truncate anything, create an extension, make a temporary table, or write `schema_migrations`.
The Postgres exporter connects as `visionquery_monitoring`, a member of `pg_monitor` that reads none of the memory's rows.

The roles are made by a migration (`0003_roles.sql`), so an existing Deployment gets them at its next `make up` like any other schema change.
The migration grants the runtime role the rows of every table there is and, through default privileges, of every table and sequence the migrating role creates later, so a later migration never grants anything for it, unless it adds a schema, on which it grants `USAGE`.
Roles belong to the server rather than to one database, so the migration makes each one only when missing, and strips one that existed of every attribute, membership, login, password, connection limit, setting and direct grant on this database or its `public` schema but those it gives.
For the same reason one Postgres server holds one memory: two memories on a server would share the runtime role, so it would read and write both, and each `visionquery-migrate` would overwrite the other's password.
Two runs of `0003` at the same moment against databases of one server can also fail with "tuple concurrently updated"; rerunning is safe.

A migration is shipped and checksummed, so it holds no password: both roles are made unable to log in.
After every run, `visionquery-migrate` gives each its login with the password it is configured with (`VISIONQUERY_RUNTIME_PASSWORD`, `VISIONQUERY_MONITORING_PASSWORD`, which Compose fills from `.env`), sending the server a SCRAM verifier made on the client, never the password.
Changing a password in `.env` therefore takes effect at the next `make up`, which reruns `migrate` before the services it changed are recreated.

## Considered Options

An init script in the Postgres image was rejected: it runs only when the data directory is first made, so an existing Deployment would never get the roles, and a changed password would never reach the server.
A separate one-shot setup service was rejected: it would need the superuser's password as `migrate` does, and every service would have to wait for both.
Passwords in the migration were rejected because a migration is shipped to every Deployment and can never be edited once applied.

## Consequences

The role `visionquery-migrate` connects as must be able to create roles and grant `pg_monitor` (in practice a superuser), as well as create the `timescaledb` and `vector` extensions.
Only `postgres` and `migrate` are given the superuser's password; `tests/test_compose.py` checks it, and that every other service connecting to the memory does so as `visionquery_runtime`.
A service that one day needs more than rows, such as a temporary table or `TRUNCATE`, gets it from a migration that grants it, so every power a service has is written down in one place.
