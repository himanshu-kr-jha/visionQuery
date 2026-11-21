"""The runner migrates only an empty database or a VisionQuery memory, never someone else's.

A URL pointed at the wrong database (another application's, or the Postgres an Operator keeps
for something else) must not get the memory's schema written into it. Every test runs against
a real Postgres (see packages/conftest.py), in a database of its own.
"""

import hashlib
import re
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
import visionquery.memory.migrate as migrate_module
from psycopg import sql
from visionquery.memory.migrate import (
    COUNTED_CATALOGS,
    MIGRATIONS,
    UNCOUNTED_CATALOGS,
    ForeignDatabaseError,
    migrate,
    status,
)


def _execute(url: str, *statements: str) -> None:
    with psycopg.connect(url, autocommit=True) as connection:
        for statement in statements:
            connection.execute(statement.encode())


def _exists(url: str, name: str) -> bool:
    with psycopg.connect(url) as connection:
        row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def _new_database(postgres: str) -> str:
    name = f"test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    return urlsplit(postgres)._replace(path=f"/{name}").geturl()


def _drop_database(postgres: str, url: str) -> None:
    name = urlsplit(url).path.lstrip("/")
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_people.sql").write_text("CREATE TABLE people (id int);")
    return directory


#: What `visionquery-migrate` makes, spelled out: an empty record of this is ours.
_RECORD_LIKE = (
    "CREATE TABLE schema_migrations ("
    " number integer PRIMARY KEY CHECK (number > 0), name text NOT NULL UNIQUE,"
    " sha256 bytea NOT NULL CHECK (octet_length(sha256) = 32),"
    " applied_at timestamptz NOT NULL DEFAULT now())"
)


def _refused(database: str, migrations: Path) -> str:
    with pytest.raises(ForeignDatabaseError) as refused:
        migrate(database, migrations)
    # Nothing was written: no table of ours, not even the record.
    assert not _exists(database, "public.people")
    return str(refused.value)


def _named(message: str) -> str:
    """The objects a refusal names, and nothing else."""
    found = re.search(r"no record of VisionQuery migrations: (.*)\); point the URL", message)
    assert found, message
    return found[1]


def test_a_database_with_unrelated_tables_and_no_record_is_refused(
    database: str, migrations: Path
) -> None:
    _execute(database, "CREATE TABLE invoices (id int)")
    message = _refused(database, migrations)
    assert "not a VisionQuery memory" in message
    assert "public.invoices" in message
    assert not _exists(database, "public.schema_migrations")


def test_a_table_in_another_schema_makes_a_database_foreign_too(
    database: str, migrations: Path
) -> None:
    _execute(database, "CREATE SCHEMA billing", "CREATE VIEW billing.totals AS SELECT 1 AS n")
    message = _refused(database, migrations)
    assert "billing.totals" in message
    # A view is not a table, so the refusal must not claim the database has tables.
    assert "it has objects and no record" in message
    assert "tables" not in message


def test_a_schema_migrations_table_that_is_not_ours_is_refused(
    database: str, migrations: Path
) -> None:
    # The shape another migration tool (golang-migrate) gives its record.
    _execute(
        database,
        "CREATE TABLE schema_migrations (version bigint PRIMARY KEY, dirty boolean NOT NULL)",
        "INSERT INTO schema_migrations VALUES (20240101, false)",
    )
    message = _refused(database, migrations)
    assert "schema_migrations table that VisionQuery did not create" in message
    with psycopg.connect(database) as connection:
        rows = connection.execute("SELECT * FROM schema_migrations").fetchall()
    assert rows == [(20240101, False)]


def test_an_empty_record_beside_unrelated_tables_is_refused(
    database: str, migrations: Path
) -> None:
    # Our record, with nothing in it, proves nothing about the tables beside it.
    _execute(
        database,
        _RECORD_LIKE,
        "CREATE TABLE invoices (id int)",
    )
    assert "public.invoices" in _refused(database, migrations)


def test_an_empty_database_is_migrated_even_with_extensions_installed(
    database: str, migrations: Path
) -> None:
    # As in a database made from the TimescaleDB image's template1, which has the extension.
    _execute(database, "CREATE EXTENSION timescaledb", "CREATE EXTENSION vector")
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_extensions_alone_do_not_make_a_database_foreign(database: str, migrations: Path) -> None:
    # Their schemas, types, functions, operators, casts, operator classes, access methods
    # (vector's hnsw), text search dictionaries and templates (unaccent), foreign-data wrappers
    # (postgres_fdw) and event triggers (timescaledb's) are members of the extensions, not
    # someone's objects.
    _execute(
        database,
        "CREATE EXTENSION timescaledb",
        "CREATE EXTENSION vector",
        "CREATE EXTENSION pg_stat_statements",
        "CREATE EXTENSION pgcrypto",
        "CREATE EXTENSION hstore",
        "CREATE EXTENSION unaccent",
        "CREATE EXTENSION postgres_fdw",
        "CREATE EXTENSION pg_trgm",
        "CREATE EXTENSION btree_gist",
        "CREATE EXTENSION citext",
    )
    assert migrate(database, migrations) == ("0001_people.sql",)


@pytest.mark.parametrize("name", ["template1", "postgres"])
def test_the_images_own_databases_are_not_foreign(
    postgres: str, migrations: Path, name: str
) -> None:
    # The stack's database is made from the TimescaleDB image's template1, which its entrypoint
    # gave the extension, as it did `postgres`; nothing else in them is someone's object. Asked
    # with `status`, which changes nothing, and not by copying template1, which TimescaleDB's
    # scheduler may be connected to.
    url = urlsplit(postgres)._replace(path=f"/{name}").geturl()
    assert status(url, migrations).pending == ("0001_people.sql",)


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (["CREATE TYPE mood AS ENUM ('sad', 'ok')"], "public.mood"),
        (["CREATE TYPE pair AS (a int, b int)"], "public.pair"),
        (["CREATE DOMAIN positive AS int CHECK (VALUE > 0)"], "public.positive"),
        (
            ["CREATE FUNCTION twice(x int) RETURNS int LANGUAGE sql AS 'SELECT x * 2'"],
            "public.twice",
        ),
        (["CREATE PROCEDURE noop() LANGUAGE sql AS 'SELECT 1'"], "public.noop"),
        (["CREATE SCHEMA billing"], "billing"),
    ],
    ids=["enum", "composite", "domain", "function", "procedure", "schema"],
)
def test_a_lone_type_function_or_schema_makes_a_database_foreign_too(
    database: str, migrations: Path, statements: list[str], named: str
) -> None:
    _execute(database, *statements)
    message = _refused(database, migrations)
    assert "it has objects and no record" in message
    assert named in message


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (
            ["CREATE OPERATOR === (LEFTARG = integer, RIGHTARG = integer, FUNCTION = int4eq)"],
            "operator public.===(integer,integer)",
        ),
        (["CREATE CAST (point AS text) WITH INOUT"], "cast (pg_catalog.point AS pg_catalog.text)"),
        (['CREATE COLLATION bytewise FROM "C"'], "collation public.bytewise"),
        (
            ["CREATE TEXT SEARCH CONFIGURATION plain (COPY = english)"],
            "text search configuration public.plain",
        ),
        (
            # Its function joins an extension, so the event trigger is the one object of its own.
            [
                "CREATE EXTENSION hstore",
                "CREATE FUNCTION stop() RETURNS event_trigger LANGUAGE plpgsql AS 'BEGIN END'",
                "ALTER EXTENSION hstore ADD FUNCTION stop()",
                "CREATE EVENT TRIGGER watch ON ddl_command_start EXECUTE FUNCTION stop()",
            ],
            "event trigger watch",
        ),
        (
            [
                "CREATE EXTENSION postgres_fdw",
                "CREATE SERVER elsewhere FOREIGN DATA WRAPPER postgres_fdw",
            ],
            "server elsewhere",
        ),
        (["CREATE PUBLICATION everything FOR ALL TABLES"], "publication everything"),
        (
            ["CREATE ACCESS METHOD heap2 TYPE TABLE HANDLER heap_tableam_handler"],
            "access method heap2",
        ),
        (
            ["CREATE CONVERSION latin FOR 'LATIN1' TO 'UTF8' FROM iso8859_1_to_utf8"],
            "conversion public.latin",
        ),
        (["CREATE FOREIGN DATA WRAPPER nowhere"], "foreign-data wrapper nowhere"),
        (["CREATE LANGUAGE plmine HANDLER plpgsql_call_handler"], "language plmine"),
        (["CREATE OPERATOR FAMILY ints USING btree"], "operator family public.ints USING btree"),
        (
            # Its operator family is made with it, and named for it.
            [
                "CREATE OPERATOR CLASS ints FOR TYPE int4 USING btree"
                " AS OPERATOR 1 <, FUNCTION 1 btint4cmp(int4, int4)"
            ],
            "operator family public.ints USING btree",
        ),
        (
            ["CREATE TEXT SEARCH DICTIONARY words (TEMPLATE = simple)"],
            "text search dictionary public.words",
        ),
        (
            ["CREATE TEXT SEARCH TEMPLATE plain (LEXIZE = dsimple_lexize, INIT = dsimple_init)"],
            "text search template public.plain",
        ),
        (
            [
                "CREATE TEXT SEARCH PARSER plain (START = prsd_start, GETTOKEN = prsd_nexttoken,"
                " END = prsd_end, LEXTYPES = prsd_lextype, HEADLINE = prsd_headline)"
            ],
            "text search parser public.plain",
        ),
        (
            [
                "CREATE EXTENSION hstore",
                "CREATE TRANSFORM FOR hstore LANGUAGE plpgsql"
                " (FROM SQL WITH FUNCTION varchar_support(internal))",
            ],
            "transform for public.hstore language plpgsql",
        ),
        # Whoever makes a large object chooses its number, so it may be a small one.
        (["SELECT lo_create(42)"], "large object 42"),
        # The catalogs' own schema is no hiding place for an object someone made.
        (
            ["CREATE FUNCTION pg_catalog.hidden() RETURNS int LANGUAGE sql AS 'SELECT 1'"],
            "function pg_catalog.hidden()",
        ),
    ],
    ids=[
        "operator",
        "cast",
        "collation",
        "text-search-configuration",
        "event-trigger",
        "foreign-server",
        "publication",
        "access-method",
        "conversion",
        "foreign-data-wrapper",
        "language",
        "operator-family",
        "operator-class",
        "text-search-dictionary",
        "text-search-template",
        "text-search-parser",
        "transform",
        "large-object",
        "function-in-pg-catalog",
    ],
)
def test_every_other_kind_of_object_makes_a_database_foreign_too(
    database: str, migrations: Path, statements: list[str], named: str
) -> None:
    _execute(database, *statements)
    message = _refused(database, migrations)
    assert _named(message) == named


@pytest.mark.parametrize(
    ("statement", "named"),
    [
        (
            "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC",
            r"default acl for role \w+ on tables",
        ),
        (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO PUBLIC",
            r"default acl for role \w+ in schema public on tables",
        ),
        (
            "ALTER DEFAULT PRIVILEGES GRANT USAGE ON SEQUENCES TO PUBLIC",
            r"default acl for role \w+ on sequences",
        ),
        (
            "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
            r"default acl for role \w+ on functions",
        ),
        (
            "ALTER DEFAULT PRIVILEGES REVOKE USAGE ON TYPES FROM PUBLIC",
            r"default acl for role \w+ on types",
        ),
        (
            "ALTER DEFAULT PRIVILEGES GRANT USAGE ON SCHEMAS TO PUBLIC",
            r"default acl for role \w+ on schemas",
        ),
        (
            "ALTER DEFAULT PRIVILEGES GRANT SELECT ON LARGE OBJECTS TO PUBLIC",
            r"default acl for role \w+ on large objects",
        ),
    ],
    ids=["tables", "tables-in-schema", "sequences", "functions", "types", "schemas", "blobs"],
)
def test_a_default_privilege_made_ahead_of_the_first_run_makes_a_database_foreign(
    database: str, migrations: Path, statement: str, named: str
) -> None:
    # A default privilege is not inert: it would be applied to every table the run makes, the
    # memory's included, so that every role of the server could read and truncate them.
    _execute(database, statement)
    message = _refused(database, migrations)
    assert re.fullmatch(named, _named(message)), message
    assert not _exists(database, "public.schema_migrations")


def test_a_default_privilege_for_another_role_makes_a_database_foreign_too(
    database: str, migrations: Path
) -> None:
    role = f"owner_{uuid.uuid4().hex[:8]}"
    _execute(database, f'CREATE ROLE "{role}"')
    try:
        _execute(
            database, f'ALTER DEFAULT PRIVILEGES FOR ROLE "{role}" GRANT ALL ON TABLES TO PUBLIC'
        )
        message = _refused(database, migrations)
        assert _named(message) == f"default acl for role {role} on tables"
    finally:
        _execute(database, f'DROP OWNED BY "{role}"', f'DROP ROLE "{role}"')


def test_a_default_privilege_taken_back_makes_nothing_of_the_database(
    database: str, migrations: Path
) -> None:
    _execute(
        database,
        "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC",
        "ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM PUBLIC",
    )
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_a_grant_on_the_public_schema_ahead_of_the_first_run_is_taken_back_by_it(
    database: str, migrations: Path
) -> None:
    # `0003_roles.sql` revokes what PUBLIC holds on the database and the schema, so it is
    # state the run accounts for.
    _execute(
        database,
        "GRANT CREATE ON SCHEMA public TO PUBLIC",
        f'GRANT ALL ON DATABASE "{urlsplit(database).path.lstrip("/")}" TO PUBLIC',
    )
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_grants_to_other_roles_ahead_of_the_first_run_give_the_memory_no_more_access(
    database: str, migrations: Path
) -> None:
    # What is left of this class (grants that are no object) cannot change who may read or write
    # the memory's tables: only an owner, a membership or an ACL can, and an ACL is made by a
    # grant on the table, a default privilege (counted above) or a role of ours (which `0003`
    # refuses to adopt). A named role's grant on the schema or database, and a setting of that
    # role's in every database (which no session of this database gets from another role), are
    # what an Operator may prepare.
    other = f"other_{uuid.uuid4().hex[:8]}"
    name = urlsplit(database).path.lstrip("/")
    _execute(
        database,
        f"CREATE ROLE \"{other}\" LOGIN PASSWORD '{other}'",
        f'GRANT ALL ON SCHEMA public TO "{other}"',
        f'GRANT ALL ON DATABASE "{name}" TO "{other}"',
        f'ALTER ROLE "{other}" SET default_transaction_read_only = off',
    )
    try:
        assert migrate(database, migrations) == ("0001_people.sql",)
        parts = urlsplit(database)
        as_other = parts._replace(netloc=f"{other}:{other}@{parts.hostname}:{parts.port}").geturl()
        with psycopg.connect(as_other, autocommit=True) as connection:
            for statement in (
                "SELECT count(*) FROM people",
                "INSERT INTO people VALUES (1)",
                "TRUNCATE people",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement.encode())
    finally:
        _execute(database, f'DROP OWNED BY "{other}"', f'DROP ROLE "{other}"')


@pytest.mark.parametrize(
    "setting",
    [
        "SET session_replication_role = replica",
        "SET role = {other}",
        "SET log_statement = 'all'",
        "SET search_path = nowhere",
    ],
    ids=["replica", "role", "log_statement", "search_path"],
)
@pytest.mark.parametrize("scope", ["database", "role in database", "all in database"])
def test_a_setting_of_the_database_makes_it_foreign_and_its_value_is_not_named(
    postgres: str, database: str, migrations: Path, setting: str, scope: str
) -> None:
    # A setting reaches every session of the memory, and `RESET ALL` returns to it: `replica`
    # turns off every foreign key and trigger, `role` and `log_statement` change who a session
    # is and what the log holds.
    other = f"other_{uuid.uuid4().hex[:8]}"
    name = urlsplit(database).path.lstrip("/")
    target = {
        "database": f'ALTER DATABASE "{name}"',
        "role in database": f'ALTER ROLE "{other}" IN DATABASE "{name}"',
        "all in database": f'ALTER ROLE ALL IN DATABASE "{name}"',
    }[scope]
    who = {
        "database": "all roles",
        "role in database": f"role {other}",
        "all in database": "all roles",
    }[scope]
    # Through `postgres`: a `role` setting makes this database's own sessions run as `other`.
    _execute(postgres, f'CREATE ROLE "{other}"')
    try:
        _execute(postgres, f"{target} {setting.format(other=other)}")
        message = _refused(database, migrations)
        assert _named(message) == f"setting for {who} in database {name}"
        for value in ("replica", "log_statement", "nowhere", "'all'"):
            assert value not in message
        with pytest.raises(ForeignDatabaseError):
            status(database, migrations)
    finally:
        _execute(postgres, f'DROP ROLE "{other}"')


@pytest.mark.parametrize(
    ("statement", "named"),
    [
        ("ALTER ROLE ALL SET log_statement = 'all'", "setting for all roles in every database"),
        ("ALTER ROLE {me} SET log_statement = 'all'", "setting for role {me} in every database"),
    ],
    ids=["all-roles", "migrating-role"],
)
def test_a_setting_in_every_database_for_everyone_or_the_migrating_role_counts(
    database: str, statement: str, named: str
) -> None:
    # Server-wide, so it is made in a transaction that is rolled back and no other test's
    # session ever sees it.
    with psycopg.connect(database) as connection:
        row = connection.execute("SELECT quote_ident(current_user)").fetchone()
        assert row
        me = str(row[0])
        connection.execute(statement.format(me=me).encode())
        reason = migrate_module._foreign(connection, database, 10)  # pyright: ignore[reportPrivateUsage]
        connection.rollback()
    assert reason is not None
    assert named.format(me=me) in reason
    assert "'all'" not in reason


def test_a_setting_for_another_role_in_every_database_is_not_this_databases(
    database: str, migrations: Path
) -> None:
    other = f"other_{uuid.uuid4().hex[:8]}"
    _execute(database, f'CREATE ROLE "{other}"', f'ALTER ROLE "{other}" SET role = none')
    try:
        assert migrate(database, migrations) == ("0001_people.sql",)
    finally:
        _execute(database, f'DROP ROLE "{other}"')


def test_an_empty_record_another_role_made_is_refused(database: str, migrations: Path) -> None:
    # Its owner would keep every right on the record, and could rewrite what was applied.
    mallory = f"mallory_{uuid.uuid4().hex[:8]}"
    _execute(
        database,
        f"CREATE ROLE \"{mallory}\" LOGIN PASSWORD '{mallory}'",
        f'GRANT ALL ON SCHEMA public TO "{mallory}"',
    )
    try:
        parts = urlsplit(database)
        as_mallory = parts._replace(
            netloc=f"{mallory}:{mallory}@{parts.hostname}:{parts.port}"
        ).geturl()
        _execute(as_mallory, _RECORD_LIKE)
        assert "schema_migrations table that VisionQuery did not create" in _refused(
            database, migrations
        )
        with pytest.raises(ForeignDatabaseError):
            status(database, migrations)
    finally:
        _execute(database, f'DROP OWNED BY "{mallory}"', f'DROP ROLE "{mallory}"')


@pytest.mark.parametrize(
    "change",
    [
        "ALTER COLUMN applied_at DROP DEFAULT",
        "ALTER COLUMN sha256 DROP NOT NULL",
        "ALTER COLUMN applied_at SET DEFAULT now() + interval '1 day'",
        "DROP CONSTRAINT schema_migrations_pkey",
        "DROP CONSTRAINT schema_migrations_name_key",
        "DROP CONSTRAINT schema_migrations_number_check",
        "DROP CONSTRAINT schema_migrations_sha256_check",
        "ADD CONSTRAINT extra CHECK (number < 1000)",
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "SET (fillfactor = 50)",
    ],
    ids=[
        "no-default",
        "nullable",
        "other-default",
        "no-primary-key",
        "no-unique-name",
        "no-number-check",
        "no-digest-check",
        "extra-check",
        "row-level-security",
        "forced-row-level-security",
        "option",
    ],
)
def test_an_empty_record_that_is_not_what_creating_it_makes_is_refused(
    database: str, migrations: Path, change: str
) -> None:
    _execute(database, _RECORD_LIKE, f"ALTER TABLE schema_migrations {change}")
    assert "schema_migrations table that VisionQuery did not create" in _refused(
        database, migrations
    )
    with pytest.raises(ForeignDatabaseError):
        status(database, migrations)


@pytest.mark.parametrize("missing", ["sha256", "applied_at"])
def test_a_record_holding_a_null_is_refused_and_status_does_not_crash(
    database: str, migrations: Path, missing: str
) -> None:
    # Ours has no NULL, so a record with one was made by someone else.
    digest = "NULL" if missing == "sha256" else "decode(repeat('00', 32), 'hex')"
    applied = "now()" if missing == "sha256" else "NULL"
    _execute(
        database,
        "CREATE TABLE schema_migrations (number integer PRIMARY KEY, name text NOT NULL UNIQUE,"
        " sha256 bytea, applied_at timestamptz)",
        "INSERT INTO schema_migrations (number, name, sha256, applied_at)"
        f" VALUES (1, '0001_people.sql', {digest}, {applied})",
    )
    with pytest.raises(ForeignDatabaseError):
        status(database, migrations)
    assert "schema_migrations table that VisionQuery did not create" in _refused(
        database, migrations
    )


def test_the_empty_record_a_failed_first_run_leaves_is_still_ours(
    database: str, migrations: Path
) -> None:
    broken = migrations.parent / "broken"
    broken.mkdir()
    (broken / "0001_people.sql").write_text("SELECT * FROM nowhere;")
    with pytest.raises(migrate_module.MigrationFailedError):
        migrate(database, broken)
    assert status(database, migrations).pending == ("0001_people.sql",)
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_the_empty_record_creating_it_makes_is_ours_and_status_reads_it(
    database: str, migrations: Path
) -> None:
    _execute(database, _RECORD_LIKE)
    assert status(database, migrations).pending == ("0001_people.sql",)
    # `status` made its comparison record in a rolled-back transaction, and left nothing.
    with psycopg.connect(database) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_class WHERE relname = 'schema_migrations_scratch'"
        ).fetchone()
    assert row == (0,)


def test_what_hangs_off_an_object_initdb_made_is_counted(database: str, migrations: Path) -> None:
    # Only what hangs off a counted object, an extension's or our record is part of it.
    _execute(
        database,
        "CREATE RULE r AS ON INSERT TO information_schema.sql_features DO INSTEAD NOTHING",
        "CREATE INDEX ON information_schema.sql_parts (feature_id)",
    )
    message = _refused(database, migrations)
    assert "rule r on information_schema.sql_features" in message
    assert "sql_parts" in message


def test_a_named_object_is_never_cut_off_by_many_that_are_gone(
    database: str, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Many objects Postgres cannot name (and that are gone when asked again) sort before the
    # one it can name; the limit must not drop the named one.
    _execute(
        database,
        "CREATE TABLE invoices (id int)",
        "CREATE FUNCTION public.partly(classid oid, objid oid, subid int)"
        " RETURNS TABLE (type text, schema text, name text, identity text)"
        " LANGUAGE sql AS $$ SELECT 'table'::text, 'public'::text, 'invoices'::text,"
        " CASE WHEN classid = 'pg_class'::regclass AND objid = 'public.invoices'::regclass"
        " THEN 'public.invoices' END $$",
        *(f"CREATE TABLE filler_{n} (id int)" for n in range(130)),
    )
    monkeypatch.setattr(
        migrate_module,
        "_OBJECTS",
        migrate_module.objects_query(sql.Identifier("public", "partly")),
    )
    monkeypatch.setattr(migrate_module, "_still_exists", _gone)
    assert _named(_refused(database, migrations)) == "table public.invoices"


def test_a_security_label_needs_a_provider_that_a_database_has_none_of(
    database: str,
) -> None:
    # A label attaches to an object, which is counted, and Postgres refuses one without a loaded
    # provider (sepgsql), so no database arrives with a label that is no object's.
    _execute(database, "CREATE TABLE labelled (id int)")
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _execute(database, "SECURITY LABEL ON TABLE labelled IS 'x'")


def test_a_subscription_makes_its_own_database_foreign_and_its_password_is_not_named(
    database: str, migrations: Path
) -> None:
    # A subscription lives in a catalog every database shares; its own database counts it.
    _execute(
        database,
        "CREATE SUBSCRIPTION feed CONNECTION 'host=nowhere dbname=d password=topsecret'"
        " PUBLICATION p WITH (connect = false, slot_name = NONE, enabled = false)",
    )
    try:
        message = _refused(database, migrations)
        assert _named(message) == "subscription feed"
        assert "topsecret" not in message
    finally:
        # A database cannot be dropped while it has a subscription.
        _execute(database, "DROP SUBSCRIPTION feed")


def test_another_databases_subscription_does_not_make_this_one_foreign(
    postgres: str, database: str, migrations: Path
) -> None:
    other = _new_database(postgres)
    try:
        _execute(
            other,
            "CREATE SUBSCRIPTION feed CONNECTION 'host=nowhere dbname=d'"
            " PUBLICATION p WITH (connect = false, slot_name = NONE, enabled = false)",
        )
        try:
            assert migrate(database, migrations) == ("0001_people.sql",)
        finally:
            _execute(other, "DROP SUBSCRIPTION feed")
    finally:
        _drop_database(postgres, other)


def test_a_foreign_servers_and_user_mappings_options_are_not_named(
    database: str, migrations: Path
) -> None:
    # Their options may hold credentials; the refusal names the objects only.
    _execute(
        database,
        "CREATE EXTENSION postgres_fdw",
        "CREATE SERVER elsewhere FOREIGN DATA WRAPPER postgres_fdw OPTIONS (dbname 'sekrit')",
        "CREATE USER MAPPING FOR postgres SERVER elsewhere"
        " OPTIONS (user 'someone', password 'hunter2')",
    )
    message = _refused(database, migrations)
    assert "server elsewhere" in message
    assert "user mapping postgres on server elsewhere" in message
    for secret in ("sekrit", "hunter2", "someone"):
        assert secret not in message


def test_what_belongs_to_a_table_is_named_through_the_table(
    database: str, migrations: Path
) -> None:
    # Its sequence, index, constraints, default, trigger, rule, policy and statistics object
    # all go with it, so the table is the one object named.
    _execute(
        database,
        "CREATE TABLE invoices (id serial PRIMARY KEY, total int DEFAULT 0 CHECK (total >= 0),"
        " due date)",
        "CREATE INDEX invoices_due ON invoices (due)",
        "CREATE TRIGGER same BEFORE UPDATE ON invoices"
        " FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
        "CREATE RULE keep AS ON DELETE TO invoices DO INSTEAD NOTHING",
        "ALTER TABLE invoices ENABLE ROW LEVEL SECURITY",
        "CREATE POLICY mine ON invoices USING (true)",
        "CREATE STATISTICS invoices_stats ON total, due FROM invoices",
    )
    message = _refused(database, migrations)
    assert _named(message) == "table public.invoices"


def test_a_hypertable_is_named_and_not_its_chunks(database: str, migrations: Path) -> None:
    _execute(
        database,
        "CREATE EXTENSION timescaledb",
        "CREATE TABLE readings (at timestamptz NOT NULL, value int)",
        "SELECT create_hypertable('readings', by_range('at'))",
        "INSERT INTO readings VALUES (now(), 1), (now() - interval '30 days', 2)",
    )
    message = _refused(database, migrations)
    assert _named(message) == "table public.readings"


def test_the_guard_looks_in_every_catalog_that_holds_a_databases_objects(database: str) -> None:
    # Every catalog of a database whose rows are objects (it has an oid) is counted or left out
    # for a stated reason, so a Postgres that adds a kind of object fails here first.
    with psycopg.connect(database) as connection:
        catalogs = {
            str(name)
            for (name,) in connection.execute(
                "SELECT c.relname FROM pg_catalog.pg_class c"
                " WHERE c.relnamespace = 'pg_catalog'::regnamespace AND c.relkind = 'r'"
                " AND NOT c.relisshared AND EXISTS (SELECT FROM pg_catalog.pg_attribute a"
                " WHERE a.attrelid = c.oid AND a.attname = 'oid' AND a.attnum > 0)"
            )
        }
    # pg_subscription is shared by every database, and counted for the one it names.
    assert catalogs == (set(COUNTED_CATALOGS) - {"pg_subscription"}) | set(UNCOUNTED_CATALOGS)
    assert not set(COUNTED_CATALOGS) & set(UNCOUNTED_CATALOGS)


def test_the_row_and_array_types_of_a_table_are_not_objects_of_their_own(
    database: str, migrations: Path
) -> None:
    # Only the table is named, though it has a row type and an array type in pg_type.
    _execute(database, "CREATE TABLE invoices (id int)")
    message = _refused(database, migrations)
    assert "public.invoices" in message
    assert "invoices[]" not in message
    assert "_invoices" not in message


def test_a_memory_whose_migrations_made_types_and_functions_keeps_being_migrated(
    database: str, migrations: Path
) -> None:
    (migrations / "0002_kinds.sql").write_text(
        "CREATE SCHEMA extra;"
        " CREATE TYPE extra.kind AS ENUM ('a', 'b');"
        " CREATE FUNCTION extra.one() RETURNS int LANGUAGE sql AS 'SELECT 1';"
    )
    assert migrate(database, migrations) == ("0001_people.sql", "0002_kinds.sql")
    (migrations / "0003_more.sql").write_text("CREATE TABLE more (id int);")
    assert migrate(database, migrations) == ("0003_more.sql",)


def test_another_sessions_temporary_objects_do_not_make_a_database_foreign(
    database: str, migrations: Path
) -> None:
    # Its temporary schemas outlive the session, so one that has ended counts no more.
    with psycopg.connect(database, autocommit=True) as other:
        other.execute("CREATE TEMP TABLE scratch (id int)")
        other.execute("CREATE FUNCTION pg_temp.scratch() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        assert migrate(database, migrations) == ("0001_people.sql",)
    (migrations / "0002_places.sql").write_text("CREATE TABLE places (id int);")
    _execute(database, "TRUNCATE schema_migrations")
    with pytest.raises(ForeignDatabaseError) as refused:
        migrate(database, migrations)
    # Only what the first run made is named, not the ended session's temporary schemas.
    assert _named(str(refused.value)) == "table public.people"


def test_a_refusal_names_three_objects_and_says_there_are_more(
    database: str, migrations: Path
) -> None:
    _execute(
        database,
        "CREATE TABLE a (id int)",
        "CREATE TABLE b (id int)",
        "CREATE TABLE c (id int)",
        "CREATE TABLE d (id int)",
    )
    assert _named(_refused(database, migrations)) == (
        "table public.a, table public.b, table public.c and more"
    )


def test_a_memory_whose_migrations_made_a_hypertable_with_chunks_keeps_being_migrated(
    database: str, migrations: Path
) -> None:
    # As 0005_segments.sql will: TimescaleDB's chunks, triggers and catalog rows follow it.
    (migrations / "0002_readings.sql").write_text(
        "CREATE EXTENSION IF NOT EXISTS timescaledb;"
        " CREATE TABLE readings (at timestamptz NOT NULL, value int);"
        " SELECT create_hypertable('readings', by_range('at'));"
        " INSERT INTO readings VALUES (now(), 1), (now() - interval '30 days', 2);"
    )
    assert migrate(database, migrations) == ("0001_people.sql", "0002_readings.sql")
    (migrations / "0003_more.sql").write_text("CREATE TABLE more (id int);")
    assert migrate(database, migrations) == ("0003_more.sql",)


def test_a_memory_with_tables_of_its_own_keeps_being_migrated(
    database: str, migrations: Path
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    (migrations / "0002_places.sql").write_text("CREATE TABLE places (id int);")
    assert migrate(database, migrations) == ("0002_places.sql",)


def test_a_child_of_an_extensions_table_is_someones_table(database: str, migrations: Path) -> None:
    # Extensions make no inheritance children, so a table that inherits from one is not theirs.
    _execute(
        database,
        "CREATE EXTENSION timescaledb",
        "CREATE TABLE invoices (customer text, amount numeric)"
        " INHERITS (_timescaledb_catalog.metadata)",
        "INSERT INTO invoices (key, value, include_in_telemetry, customer, amount)"
        " VALUES ('k', 'v', false, 'acme', 10)",
    )
    assert _named(_refused(database, migrations)) == "table public.invoices"


def test_a_partition_of_an_extensions_table_is_someones_table(
    database: str, migrations: Path
) -> None:
    _execute(
        database,
        "CREATE EXTENSION hstore",
        "CREATE TABLE ledger (at date NOT NULL, amount numeric) PARTITION BY RANGE (at)",
        "ALTER EXTENSION hstore ADD TABLE ledger",
        "CREATE TABLE ledger_2024 PARTITION OF ledger"
        " FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
    )
    assert _named(_refused(database, migrations)) == "table public.ledger_2024"


def test_a_child_of_a_record_that_is_not_ours_to_trust_is_someones_table(
    database: str, migrations: Path
) -> None:
    # An empty record of our shape proves nothing about what hangs off it.
    _execute(
        database,
        _RECORD_LIKE,
        "CREATE TABLE invoices (customer text, amount numeric) INHERITS (public.schema_migrations)",
    )
    assert _named(_refused(database, migrations)) == "table public.invoices"


@pytest.mark.parametrize(
    ("statement", "named"),
    [
        (
            "CREATE RULE swallow AS ON INSERT TO schema_migrations DO INSTEAD NOTHING",
            "rule swallow on public.schema_migrations",
        ),
        (
            "CREATE TRIGGER same BEFORE UPDATE ON schema_migrations"
            " FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
            "trigger same on public.schema_migrations",
        ),
        (
            "CREATE POLICY block ON schema_migrations USING (false)",
            "policy block on public.schema_migrations",
        ),
    ],
    ids=["rule", "trigger", "policy"],
)
def test_what_changes_what_a_write_to_an_empty_record_does_is_counted(
    database: str, migrations: Path, statement: str, named: str
) -> None:
    _execute(database, _RECORD_LIKE, statement)
    assert _named(_refused(database, migrations)) == named


def test_an_empty_record_with_what_creating_it_makes_does_not_make_a_database_foreign(
    database: str, migrations: Path
) -> None:
    # Its key, constraints, default, row type and toast table are made with it.
    _execute(database, _RECORD_LIKE)
    assert migrate(database, migrations) == ("0001_people.sql",)


def test_children_are_named_through_their_counted_parent_at_every_depth(
    database: str, migrations: Path
) -> None:
    _execute(
        database,
        "CREATE TABLE ledger (at date NOT NULL, amount numeric) PARTITION BY RANGE (at)",
        "CREATE TABLE ledger_2024 PARTITION OF ledger FOR VALUES FROM ('2024-01-01')"
        " TO ('2025-01-01') PARTITION BY RANGE (at)",
        "CREATE TABLE ledger_2024_h1 PARTITION OF ledger_2024 FOR VALUES FROM ('2024-01-01')"
        " TO ('2024-07-01')",
        "CREATE TABLE parent (id int)",
        "CREATE TABLE child (extra int) INHERITS (parent)",
        "CREATE TABLE grandchild (more int) INHERITS (child)",
    )
    assert _named(_refused(database, migrations)) == "table public.ledger, table public.parent"


def test_an_object_postgres_cannot_name_is_named_by_its_catalog_and_oid(
    database: str, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If `pg_identify_object` ever answers with no identity for an object that exists, the guard
    # must still count it.
    _execute(
        database,
        "CREATE FUNCTION public.unnamed(classid oid, objid oid, subid int)"
        " RETURNS TABLE (type text, schema text, name text, identity text)"
        " LANGUAGE sql AS $$ SELECT 'thing'::text, NULL::text, NULL::text, NULL::text $$",
        "CREATE TABLE invoices (id int)",
    )
    monkeypatch.setattr(
        migrate_module,
        "_OBJECTS",
        migrate_module.objects_query(sql.Identifier("public", "unnamed")),
    )
    message = _refused(database, migrations)
    assert re.search(r"pg_class \d+", message), message
    assert re.search(r"pg_proc \d+", message), message


def _gone(connection: psycopg.Connection, catalog: str, objid: int) -> bool:
    return False


def test_an_object_dropped_since_the_scan_is_left_out(
    database: str, migrations: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _execute(
        database,
        "CREATE FUNCTION public.unnamed(classid oid, objid oid, subid int)"
        " RETURNS TABLE (type text, schema text, name text, identity text)"
        " LANGUAGE sql AS $$ SELECT 'thing'::text, NULL::text, NULL::text, NULL::text $$",
    )
    monkeypatch.setattr(
        migrate_module,
        "_OBJECTS",
        migrate_module.objects_query(sql.Identifier("public", "unnamed")),
    )
    # Every object looks unnamed, and none is there any more when asked again.
    monkeypatch.setattr(migrate_module, "_still_exists", _gone)
    assert migrate(database, migrations) == ("0001_people.sql",)


@contextmanager
def _mallory(database: str) -> Generator[str]:
    """An ordinary role that may create in `public`, and the URL to log in as it."""
    name = f"mallory_{uuid.uuid4().hex[:8]}"
    _execute(
        database,
        f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{name}'",
        f'GRANT ALL ON SCHEMA public TO "{name}"',
    )
    try:
        parts = urlsplit(database)
        yield parts._replace(netloc=f"{name}:{name}@{parts.hostname}:{parts.port}").geturl()
    finally:
        _execute(database, f'DROP OWNED BY "{name}"', f'DROP ROLE "{name}"')


def _owner(database: str, relation: str) -> str:
    with psycopg.connect(database) as connection:
        row = connection.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass", (relation,)
        ).fetchone()
    assert row
    return str(row[0])


_NOT_OURS = "schema_migrations table that VisionQuery did not create"


def test_a_populated_record_another_role_made_is_refused(database: str, migrations: Path) -> None:
    # One INSERT of a public digest must not turn a look-alike into the memory's record.
    digest = hashlib.sha256((migrations / "0001_people.sql").read_bytes()).hexdigest()
    with _mallory(database) as as_mallory:
        _execute(
            as_mallory,
            _RECORD_LIKE,
            "INSERT INTO schema_migrations (number, name, sha256)"
            f" VALUES (1, '0001_people.sql', decode('{digest}', 'hex'))",
            "CREATE TABLE people (id int)",
        )
        with pytest.raises(ForeignDatabaseError) as refused:
            migrate(database, migrations)
        assert _NOT_OURS in str(refused.value)
        with pytest.raises(ForeignDatabaseError):
            status(database, migrations)
        # Nothing was written, and the planted table is still hers alone.
        with psycopg.connect(database) as connection:
            row = connection.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'visionquery_%'"
            ).fetchone()
        assert row == (0,)


@pytest.mark.parametrize(
    "change",
    [
        "DROP CONSTRAINT schema_migrations_pkey",
        "ALTER COLUMN applied_at DROP DEFAULT",
        "ENABLE ROW LEVEL SECURITY",
        "SET (fillfactor = 50)",
    ],
    ids=["no-primary-key", "no-default", "row-level-security", "option"],
)
def test_a_populated_record_that_is_not_what_creating_it_makes_is_refused(
    database: str, migrations: Path, change: str
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    _execute(database, f"ALTER TABLE schema_migrations {change}")
    (migrations / "0002_places.sql").write_text("CREATE TABLE places (id int);")
    with pytest.raises(ForeignDatabaseError) as refused:
        migrate(database, migrations)
    assert _NOT_OURS in str(refused.value)
    with pytest.raises(ForeignDatabaseError):
        status(database, migrations)
    assert not _exists(database, "public.places")


def test_a_populated_record_whose_owner_is_not_the_migrating_role_is_refused(
    database: str, migrations: Path
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    with _mallory(database):
        # The record is genuine, but its owner could rewrite what was applied.
        mallory = _owner_candidate(database)
        _execute(database, f'ALTER TABLE schema_migrations OWNER TO "{mallory}"')
        with pytest.raises(ForeignDatabaseError) as refused:
            migrate(database, migrations)
        assert _NOT_OURS in str(refused.value)


def _owner_candidate(database: str) -> str:
    with psycopg.connect(database) as connection:
        row = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE 'mallory_%'"
        ).fetchone()
    assert row
    return str(row[0])


@pytest.mark.parametrize(
    "plant",
    [
        "CREATE TABLE {n}.cameras (id int)",
        "CREATE VIEW {n}.cameras AS SELECT 1 AS id",
        "CREATE MATERIALIZED VIEW {n}.cameras AS SELECT 1 AS id",
        "CREATE SEQUENCE {n}.cameras",
        "CREATE FOREIGN TABLE {n}.cameras (id int) SERVER nowhere",
    ],
    ids=["table", "view", "materialized-view", "sequence", "foreign-table"],
)
def test_a_relation_another_role_owns_makes_a_genuine_memory_foreign(
    database: str, migrations: Path, plant: str
) -> None:
    # Planted under a genuine record, to be written to and read by the memory's services.
    assert migrate(database, migrations) == ("0001_people.sql",)
    (migrations / "0002_places.sql").write_text("CREATE TABLE places (id int);")
    with _mallory(database) as as_mallory:
        mallory = _owner_candidate(database)
        _execute(database, f'GRANT CREATE ON SCHEMA public TO "{mallory}"')
        if "FOREIGN" in plant:
            _execute(
                database,
                "CREATE FOREIGN DATA WRAPPER wrapper",
                "CREATE SERVER nowhere FOREIGN DATA WRAPPER wrapper",
                f'GRANT USAGE ON FOREIGN SERVER nowhere TO "{mallory}"',
            )
        _execute(as_mallory, plant.format(n="public"))
        with pytest.raises(ForeignDatabaseError) as refused:
            migrate(database, migrations)
        assert "public.cameras" in str(refused.value)
        with pytest.raises(ForeignDatabaseError):
            status(database, migrations)
        assert not _exists(database, "public.places")


def test_a_relation_another_role_owns_in_any_schema_makes_a_genuine_memory_foreign(
    database: str, migrations: Path
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    with _mallory(database) as as_mallory:
        mallory = _owner_candidate(database)
        _execute(database, "CREATE SCHEMA extra", f'GRANT ALL ON SCHEMA extra TO "{mallory}"')
        _execute(as_mallory, "CREATE TABLE extra.cameras (id int)")
        with pytest.raises(ForeignDatabaseError) as refused:
            migrate(database, migrations)
        assert "extra.cameras" in str(refused.value)


def test_a_memorys_own_table_handed_to_another_role_is_refused(
    database: str, migrations: Path
) -> None:
    assert migrate(database, migrations) == ("0001_people.sql",)
    with _mallory(database):
        _execute(database, f'ALTER TABLE people OWNER TO "{_owner_candidate(database)}"')
        with pytest.raises(ForeignDatabaseError) as refused:
            migrate(database, migrations)
        assert "public.people" in str(refused.value)


def test_a_real_memory_keeps_migrating_and_reporting_its_status(database: str) -> None:
    # Everything the shipped migrations make is owned by the migrating role, through 0004.
    done = migrate(database, MIGRATIONS)
    assert done[-1] == "0004_camera_publish_credentials.sql"
    assert migrate(database, MIGRATIONS) == ()
    assert status(database, MIGRATIONS).pending == ()
    assert _owner(database, "public.cameras") == _owner(database, "public.schema_migrations")
