"""Gates the one thing `init_schema` cannot check itself: that a database *upgraded* in place
ends up with the same constraints as one created fresh.

`schema.sql` defines every table for a new database; `_COLUMNS_TO_ENSURE` re-defines a subset of
those columns for an existing database that predates them. Nothing tied the two together, and
three had already drifted -- `schedule_mode`, `is_active` and `run_kind` carried a CHECK in
`schema.sql` and none in the upgrade path, so the production database (which is upgraded, never
fresh) accepted values every test rejected.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from beehive.db.connection import _COLUMNS_TO_ENSURE, connect, init_schema


def _create_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    assert row is not None, f"schema.sql does not define a table named {table!r}"
    return row[0]


def _column_definition(create_sql: str, column: str) -> str:
    """The one column's definition text, with the CREATE TABLE's own line breaks and repeated
    spaces collapsed so it can be compared to the single-line fragment in _COLUMNS_TO_ENSURE."""
    body = create_sql[create_sql.index("(") + 1:create_sql.rindex(")")]
    definitions = []
    depth = 0
    quote = ""
    current = ""
    for char in body:
        if quote:
            # Inside a string/identifier literal, so parentheses and commas are just text.
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            definitions.append(current)
            current = ""
            continue
        current += char
    definitions.append(current)

    for definition in definitions:
        normalized = " ".join(definition.split())
        if normalized.split(" ", 1)[0].strip('"') == column:
            return normalized
    raise AssertionError(f"no column {column!r} in:\n{create_sql}")


def _constraints(definition: str) -> set[str]:
    """The constraint clauses of a column definition, order-independent and case-normalized."""
    upper = definition.upper()
    found = set()
    if "NOT NULL" in upper:
        found.add("NOT NULL")
    default = re.search(r"DEFAULT\s+('[^']*'|\([^)]*\)|\S+)", definition, re.IGNORECASE)
    if default:
        found.add(f"DEFAULT {default.group(1)}")
    for check in re.findall(r"CHECK\s*\((.*)\)", definition, re.IGNORECASE):
        found.add("CHECK " + " ".join(check.upper().split()))
    return found


@pytest.fixture
def fresh_conn(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    init_schema(conn)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    "table,column,ensure_ddl",
    _COLUMNS_TO_ENSURE,
    ids=[f"{table}.{column}" for table, column, _ in _COLUMNS_TO_ENSURE],
)
def test_ensured_column_matches_its_schema_sql_definition(
    fresh_conn, table, column, ensure_ddl
):
    schema_definition = _column_definition(_create_table_sql(fresh_conn, table), column)
    # Drop the leading column name so only the type + constraints are compared.
    schema_ddl = schema_definition.split(" ", 1)[1] if " " in schema_definition else ""

    assert _constraints(schema_ddl) == _constraints(ensure_ddl), (
        f"{table}.{column} differs between schema.sql (a fresh database) and "
        f"_COLUMNS_TO_ENSURE (an upgraded one):\n"
        f"  schema.sql:          {schema_ddl}\n"
        f"  _COLUMNS_TO_ENSURE:  {ensure_ddl}"
    )


@pytest.mark.parametrize(
    "table,column,ensure_ddl",
    _COLUMNS_TO_ENSURE,
    ids=[f"{table}.{column}" for table, column, _ in _COLUMNS_TO_ENSURE],
)
def test_ensured_column_type_matches_schema_sql(fresh_conn, table, column, ensure_ddl):
    schema_definition = _column_definition(_create_table_sql(fresh_conn, table), column)
    schema_type = schema_definition.split(" ")[1].upper()
    assert schema_type == ensure_ddl.split(" ")[0].upper(), (
        f"{table}.{column}: schema.sql says {schema_type}, _COLUMNS_TO_ENSURE says "
        f"{ensure_ddl.split(' ')[0]}")


def test_an_upgraded_database_enforces_the_same_checks_as_a_fresh_one(tmp_path):
    """End-to-end version of the parametrized checks above: build a table the way a legacy
    database has it (no CHECK), run init_schema, and confirm the constraint now bites."""
    path = str(tmp_path / "legacy.db")
    legacy = connect(path)
    legacy.executescript(
        """
        CREATE TABLE email_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            subject_template TEXT NOT NULL DEFAULT '',
            recipient_email TEXT,
            send_interval_hours INTEGER NOT NULL DEFAULT 24,
            last_sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        );
        """
    )
    legacy.commit()

    init_schema(legacy)

    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute(
            "INSERT INTO email_groups (name, schedule_mode) VALUES ('g', 'not-a-mode')")
    legacy.close()
