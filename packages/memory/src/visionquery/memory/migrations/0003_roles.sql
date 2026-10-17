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
-- Roles belong to the server, not to one database, so either may exist already (another
-- database on the server was migrated first, or someone made it): each is made only when
-- missing, and stripped of any power, membership, login, password, limit, setting and direct grant on this
-- database it has but those given here. Both are made unable to log in and without a password: visionquery-migrate gives each its login, with the
-- password it is configured with (visionquery.memory.roles), because a migration is shipped
-- and must hold no secret.
DO $$
DECLARE
    role_name text;
    membership record;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['visionquery_runtime', 'visionquery_monitoring'] LOOP
        BEGIN
            EXECUTE format('CREATE ROLE %I NOLOGIN', role_name);
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;  -- made already, perhaps just now by a run against another database
        END;
        EXECUTE format(
            'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT'
            ' NOLOGIN PASSWORD NULL CONNECTION LIMIT -1 VALID UNTIL ''infinity''',
            role_name
        );
        EXECUTE format('ALTER ROLE %I RESET ALL', role_name);
        EXECUTE format('ALTER ROLE %I IN DATABASE %I RESET ALL', role_name, current_database());
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), role_name);
        EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
        FOR membership IN
            SELECT g.rolname AS granted, b.rolname AS grantor
            FROM pg_auth_members m
            JOIN pg_roles r ON r.oid = m.member
            JOIN pg_roles g ON g.oid = m.roleid
            JOIN pg_roles b ON b.oid = m.grantor
            WHERE r.rolname = role_name
        LOOP
            EXECUTE format(
                'REVOKE %I FROM %I GRANTED BY %I CASCADE',
                membership.granted, role_name, membership.grantor
            );
        END LOOP;
    END LOOP;
END
$$;

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
