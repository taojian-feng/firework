from src.schema import build_schema_context


def test_schema_context_has_docs_values_and_keys(conn):
    text = build_schema_context(conn, "data/database_description")
    assert "A3 TEXT  -- region" in text
    assert "'south Moravia'" in text  # real values, not just docs
    assert "'DISPONENT'" in text
    assert "FOREIGN KEY district_id -> district.district_id" in text
    assert 'TABLE "order"' in text  # reserved word is quoted


def test_schema_works_without_descriptions(conn):
    text = build_schema_context(conn, None)
    assert "TABLE loan" in text and "region" not in text
