"""The runner migrates only an empty database or a VisionQuery memory, never someone else's.

A URL pointed at the wrong database (another application's, or the Postgres an Operator keeps
for something else) must not get the memory's schema written into it. Every test runs against
a real Postgres (see packages/conftest.py), in a database of its own.
"""

import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
import visionquery.memory.migrate as migrate_module
from psycopg import sql
from visionquery.memory.migrate import (
    COUNTED_CATALOGS,
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
        "CREATE TABLE schema_migrations (number integer PRIMARY KEY, name text NOT NULL UNIQUE,"
        " sha256 bytea NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())",
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


def test_settings_and_grants_to_other_roles_ahead_of_the_first_run_give_the_memory_no_more_access(
    database: str, migrations: Path
) -> None:
    # What is left of this class (state that is no object) cannot change who may read or write
    # the memory's tables: only an owner, a membership or an ACL can, and an ACL is made by a
    # grant on the table, a default privilege (counted above) or a role of ours (which `0003`
    # refuses to adopt). A named role's grant on the schema or database, and a setting, are
    # what an Operator may prepare.
    other = f"other_{uuid.uuid4().hex[:8]}"
    name = urlsplit(database).path.lstrip("/")
    _execute(
        database,
        f"CREATE ROLE \"{other}\" LOGIN PASSWORD '{other}'",
        f'GRANT ALL ON SCHEMA public TO "{other}"',
        f'GRANT ALL ON DATABASE "{name}" TO "{other}"',
        f'ALTER DATABASE "{name}" SET search_path = public',
        f'ALTER DATABASE "{name}" SET row_security = on',
        f'ALTER ROLE "{other}" SET default_transaction_read_only = off',
        f'ALTER ROLE ALL IN DATABASE "{name}" SET session_replication_role = origin',
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


_RECORD_LIKE = (
    "CREATE TABLE schema_migrations (number integer PRIMARY KEY, name text NOT NULL UNIQUE,"
    " sha256 bytea NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
)


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
