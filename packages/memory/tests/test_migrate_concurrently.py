"""Two runners started at once, as two processes each with its own connection, take turns.

Without the advisory lock both would read the same empty record, both would apply 0001, and
the second would fail on a table that already exists, or worse, apply a migration twice.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import psycopg
from visionquery.memory.migrate import LOCK_KEY, migrate

RUNNER = """
import json
import sys
from pathlib import Path

from visionquery.memory.migrate import migrate

url, directory = sys.argv[1], Path(sys.argv[2])
print("ready", flush=True)
sys.stdin.readline()  # both start migrating at the same moment
print(json.dumps(migrate(url, directory)), flush=True)
"""

STEPS = 6


def test_two_runners_started_at_once_apply_each_migration_exactly_once(
    database: str, tmp_path: Path
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # Not idempotent on purpose: a second application would fail or add a second row.
    (migrations / "0001_hits.sql").write_text("CREATE TABLE hits (number int);")
    for number in range(2, STEPS + 1):
        (migrations / f"{number:04d}_hit.sql").write_text(
            f"SELECT pg_sleep(0.2);\nINSERT INTO hits VALUES ({number});"
        )

    runners = [
        subprocess.Popen(
            [sys.executable, "-c", RUNNER, database, str(migrations)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for runner in runners:
        assert runner.stdout is not None
        assert runner.stdout.readline() == "ready\n"
    for runner in runners:
        assert runner.stdin is not None
        runner.stdin.write("go\n")
        runner.stdin.flush()
    results = [runner.communicate(timeout=60) for runner in runners]

    for runner, (_, stderr) in zip(runners, results, strict=True):
        assert runner.returncode == 0, stderr
    applied = [name for stdout, _ in results for name in json.loads(stdout)]
    assert sorted(applied) == sorted(p.name for p in migrations.iterdir())

    with psycopg.connect(database) as connection:
        hits = connection.execute("SELECT number FROM hits ORDER BY 1").fetchall()
        recorded = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert hits == [(number,) for number in range(2, STEPS + 1)]
    assert recorded == (STEPS,)


def test_a_run_waits_while_another_holds_the_lock(database: str, tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_people.sql").write_text("CREATE TABLE people (id int);")
    applied: list[tuple[str, ...]] = []

    with psycopg.connect(database, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        run = threading.Thread(target=lambda: applied.append(migrate(database, migrations)))
        run.start()
        run.join(timeout=1)
        assert run.is_alive(), "the run did not wait for the lock"
        row = other.execute("SELECT to_regclass('people') IS NULL").fetchone()
        assert row == (True,)
        other.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        run.join(timeout=30)

    assert applied == [("0001_people.sql",)]
