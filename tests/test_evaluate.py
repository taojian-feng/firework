import json

from src.db import execute_sql
from src.evaluate import QUESTIONS_PATH, extract_sql, gold_rows, is_ordered, results_match


def test_float_rounding_and_unordered_match():
    assert results_match([(4190.6642,)], [(4190.66,)], ordered=False)
    assert results_match([(2, "b"), (1, "a")], [(1, "a"), (2, "b")], ordered=False)
    assert not results_match([(2, "b"), (1, "a")], [(1, "a"), (2, "b")], ordered=True)


def test_lenient_allows_extra_columns():
    pred = [(7, "Brno", 128)]
    gold = [("Brno", 128)]
    assert not results_match(pred, gold, ordered=True)
    assert results_match(pred, gold, ordered=True, lenient=True)


def test_extract_sql():
    assert extract_sql("Here you go:\n```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("The query is SELECT 2") == "SELECT 2"


def test_gold_sql_matches_expected_results(conn):
    """Sanity check of the scorer itself: every gold query must score as a match."""
    for question in json.loads(QUESTIONS_PATH.read_text()):
        result = execute_sql(conn, question["gold_sql"])
        assert results_match(result.rows, gold_rows(question), is_ordered(question["gold_sql"])), question["id"]
