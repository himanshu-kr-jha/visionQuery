"""The lexer that finds transaction control in a migration file needs no database."""

import pytest
from visionquery.memory._sql import transaction_control


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS begin, atomic FROM t; END;",
        "SELECT begin atomic FROM t; END;",
        "CREATE TABLE begin_atomic (atomic int); END;",
        "INSERT INTO t VALUES (1) RETURNING begin, atomic; COMMIT;",
    ],
)
def test_begin_atomic_outside_a_routine_does_not_excuse_an_end(sql: str) -> None:
    assert transaction_control(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE FUNCTION g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;",
        "CREATE OR REPLACE FUNCTION g() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;",
        "CREATE PROCEDURE p() LANGUAGE sql BEGIN ATOMIC SELECT 1; SELECT 2; END;",
        "create or replace procedure p() language sql begin atomic select 1; end;",
    ],
)
def test_the_end_of_a_routine_body_is_allowed(sql: str) -> None:
    assert transaction_control(sql) is None
