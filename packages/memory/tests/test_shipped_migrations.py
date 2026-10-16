"""The migrations VisionQuery ships, checked without a database.

Two branches that each add a differently named 0005 merge cleanly in git and only meet here.
"""

from visionquery.memory import migrate as runner
from visionquery.memory.migrate import MIGRATIONS


def test_the_shipped_migrations_are_readable_numbered_and_free_of_transaction_control() -> None:
    files, misnamed = runner._read(MIGRATIONS)  # pyright: ignore[reportPrivateUsage]
    assert files, "no migrations are shipped"
    assert misnamed == []
    assert runner._numbering(files, []) == []  # pyright: ignore[reportPrivateUsage]
    assert runner._transaction_control(files) == []  # pyright: ignore[reportPrivateUsage]


def test_the_shipped_migrations_have_no_carriage_returns() -> None:
    # Their checksums are of their bytes, so a checkout that rewrote line endings would make
    # every applied migration look edited (see .gitattributes).
    for path in sorted(MIGRATIONS.glob("*.sql")):
        assert b"\r" not in path.read_bytes(), f"{path.name} has CR bytes"
