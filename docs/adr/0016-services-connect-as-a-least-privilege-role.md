# Services connect to the memory as a least-privilege role; only migrating is a superuser

Only `visionquery-migrate` connects to Postgres as the superuser that owns the memory.
Every long-running VisionQuery service connects as `visionquery_runtime`, which may connect to the memory's database and read and write the rows of its tables, and nothing more: it cannot create, alter, drop or truncate anything, create an extension, make a temporary table, or write `schema_migrations`.
The Postgres exporter connects as `visionquery_monitoring`, a member of `pg_monitor` that reads none of the memory's rows.

The roles are made by a migration (`0003_roles.sql`), so an existing Deployment gets them at its next `make up` like any other schema change.
The migration grants the runtime role the rows of every table there is and, through default privileges, of every table and sequence the migrating role creates later, so a later migration never grants anything for it, unless it adds a schema, on which it grants `USAGE`.
Roles belong to the server rather than to one database, so one Postgres server holds one memory: two memories on a server would share the runtime role, so it would read and write both, and each `visionquery-migrate` would overwrite the other's password.
The migration therefore makes both roles fresh and refuses when either already exists, whoever made it: it raises an exception naming the role(s), so it fails, is rolled back with nothing applied (earlier migrations stay), and `visionquery-migrate` exits 1 naming `0003_roles.sql`.
It does not clean a role it did not make, because what such a role holds (attributes, grants, default privileges, owned objects, other roles' membership in it and `ADMIN` option on it) cannot be known to be all found and taken back, and trusting it would be worse.
The operator drops the role by hand (`DROP OWNED BY` in each database that holds anything of it, then `DROP ROLE`), once nothing needs it, or gives the memory a server of its own, and migrates again.
Migrating a memory that already applied `0003` does not refuse, because `0003` is not run again.

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
