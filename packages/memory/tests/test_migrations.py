"""The migrations VisionQuery ships: where they live, what the first one does, and the wheel."""

import os
import re
import subprocess
import zipfile
from pathlib import Path

import psycopg
from visionquery.memory.migrate import MIGRATIONS, migrate

ROOT = Path(__file__).resolve().parents[3]


def test_the_shipped_migrations_live_inside_the_package() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "visionquery" / "memory"
    assert package / "migrations" == MIGRATIONS
    assert sorted(p.name for p in MIGRATIONS.glob("*.sql"))[0] == "0001_extensions.sql"


def test_the_first_migration_enables_timescaledb_and_vector(database: str) -> None:
    # The database comes from template0, so neither extension is there before migrating.
    with psycopg.connect(database) as connection:
        assert connection.execute("SELECT extname FROM pg_extension").fetchall() == [("plpgsql",)]

    assert migrate(database)[0] == "0001_extensions.sql"

    with psycopg.connect(database) as connection:
        installed = {
            name for (name,) in connection.execute("SELECT extname FROM pg_extension").fetchall()
        }
        assert {"timescaledb", "vector"} <= installed
        # Each is usable, not merely listed: a vector distance and a hypertable.
        distance = connection.execute("SELECT '[1,2,3]'::vector <-> '[1,2,5]'::vector").fetchone()
        assert distance == (2.0,)
        connection.execute("CREATE TABLE probe (at timestamptz NOT NULL, look vector(3))")
        connection.execute("SELECT create_hypertable('probe', by_range('at', interval '1 day'))")
        connection.rollback()


def test_the_wheel_carries_the_migrations(tmp_path: Path) -> None:
    subprocess.run(
        [
            os.environ.get("UV", "uv"),
            "build",
            "--package",
            "visionquery-memory",
            "--wheel",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("visionquery_memory-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        scripts = archive.read(entry_points).decode()
    shipped = sorted(n for n in names if re.fullmatch(r"visionquery/memory/migrations/.+", n))
    present = sorted(MIGRATIONS.iterdir())
    assert shipped == [f"visionquery/memory/migrations/{p.name}" for p in present]
    assert "visionquery-migrate = visionquery.memory.migrate:main" in scripts
