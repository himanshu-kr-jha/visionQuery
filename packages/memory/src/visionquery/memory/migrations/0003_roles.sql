-- The roles a Deployment's processes connect as (ADR 0016), so that only visionquery-migrate
-- connects as the superuser that owns the memory.
--
-- visionquery_runtime, for every long-running service: it reads and writes the rows of the
-- memory's tables, those of today and those later migrations create, and nothing more. It
-- cannot create, alter, drop or truncate anything, create an extension, or write
-- schema_migrations, which the runner seals after every run.
-- visionquery_monitoring, for the Postgres exporter: pg_monitor's statistics and settings, and
-- none of the memory's rows.
--
-- Roles belong to the server, not to one database, so one Postgres server holds one memory:
-- this migration refuses when either role exists already (another database on the server was
-- migrated first, or someone made it), because a role it did not make may hold powers that
-- cannot all be found and taken back, and a migration does not guess. It fails and is rolled
-- back, applying nothing, naming the roles; the operator drops them by hand (DROP OWNED BY and
-- DROP ROLE) once nothing needs them, and migrates again. Both are made unable to log in and
-- without a password: visionquery-migrate gives each its login, with the password it is
-- configured with (visionquery.memory.roles), because a migration is shipped and must hold no
-- secret.
DO $$
DECLARE
    existing text;
BEGIN
    SELECT string_agg(rolname, ', ' ORDER BY rolname) INTO existing
    FROM pg_roles
    WHERE rolname IN ('visionquery_runtime', 'visionquery_monitoring');
    IF existing IS NOT NULL THEN
        RAISE EXCEPTION
            'role(s) % already exist on this server: one Postgres server holds one memory, '
            'and a role not made by this migration is not trusted; drop it by hand, then migrate again',
            existing;
    END IF;
END
$$;

CREATE ROLE visionquery_runtime NOLOGIN;
CREATE ROLE visionquery_monitoring NOLOGIN;

GRANT pg_monitor TO visionquery_monitoring;

-- Only the roles given it may connect to the memory, and nobody may make temporary tables in it
-- or objects in its public schema (the owner and superusers aside).
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO visionquery_runtime, visionquery_monitoring',
        current_database()
    );
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO visionquery_runtime;

-- The rows of every table there is, and of every table and sequence the migrating role creates
-- from now on, in any schema of this database; a migration that adds a schema grants USAGE on
-- it. The record is only read: the runner takes back the rest on every run.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO visionquery_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO visionquery_runtime;
REVOKE INSERT, UPDATE, DELETE ON public.schema_migrations FROM visionquery_runtime;
ALTER DEFAULT PRIVILEGES GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO visionquery_runtime;
ALTER DEFAULT PRIVILEGES GRANT USAGE, SELECT ON SEQUENCES TO visionquery_runtime;
