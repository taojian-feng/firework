import sqlite3

import pytest

from src.db import UnsafeSQLError, check_sql_is_safe, execute_sql, normalize_sql


def test_normalize_strips_fences_comments_and_semicolon():
    assert normalize_sql("```sql\nSELECT 1; -- hi\n```") == "SELECT 1"


@pytest.mark.parametrize("sql", ["DELETE FROM loan", "DROP TABLE loan", "SELECT 1; DROP TABLE loan", ""])
def test_rejects_unsafe_sql(sql):
    with pytest.raises(UnsafeSQLError):
        check_sql_is_safe(sql)


def test_allows_cte():
    assert check_sql_is_safe("WITH x AS (SELECT 1) SELECT * FROM x;").startswith("WITH")


def test_connection_is_read_only(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE hack (x)")


def test_errors_are_returned_not_raised(conn):
    result = execute_sql(conn, "SELECT no_such_column FROM loan")
    assert not result.ok and "no such column" in result.error


def test_row_cap(conn):
    result = execute_sql(conn, "SELECT trans_id FROM trans", max_rows=10)
    assert len(result.rows) == 10 and result.truncated


def test_timeout(conn):
    result = execute_sql(conn, "SELECT COUNT(*) FROM trans a, trans b", timeout_s=0.2)
    assert not result.ok and "timeout" in result.error
