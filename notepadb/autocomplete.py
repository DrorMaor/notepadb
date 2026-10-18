# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""
Context-aware SQL autocomplete.

Given the full text of the editor and the cursor position, figure out what
the user is probably typing (a table name after FROM/JOIN/INTO/UPDATE, a
column name after "alias.", or just a generic word) and return a ranked list
of suggestions plus the text range that should be replaced on accept.
"""

import re

SQL_KEYWORDS = [
    "SELECT", "FROM", "WHERE", "JOIN", "INNER JOIN", "LEFT JOIN", "RIGHT JOIN",
    "FULL JOIN", "ON", "GROUP BY", "ORDER BY", "HAVING", "INSERT INTO",
    "VALUES", "UPDATE", "SET", "DELETE", "CREATE TABLE", "ALTER TABLE",
    "DROP TABLE", "DISTINCT", "TOP", "AS", "AND", "OR", "NOT", "NULL",
    "IS NULL", "IS NOT NULL", "IN", "EXISTS", "BETWEEN", "LIKE", "UNION",
    "UNION ALL", "CASE", "WHEN", "THEN", "ELSE", "END", "COUNT", "SUM",
    "AVG", "MIN", "MAX", "OVER", "PARTITION BY", "WITH", "DESC", "ASC",
]

_IDENT_RE = re.compile(r"[A-Za-z0-9_\.\[\]]*$")
_TABLE_CONTEXT_RE = re.compile(
    r"(FROM|JOIN|INTO|UPDATE|TABLE)\s+$", re.IGNORECASE
)
_ALIAS_DEF_RE = re.compile(
    r"([A-Za-z0-9_\.\[\]]+)\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?=,|\bJOIN\b|\bWHERE\b|\bON\b|\bGROUP\b|\bORDER\b|\n|$)",
    re.IGNORECASE,
)


def _current_word(text_before_cursor: str) -> str:
    match = _IDENT_RE.search(text_before_cursor)
    return match.group(0) if match else ""


def _strip_brackets(name: str) -> str:
    return name.strip("[]")


def build_alias_map(full_text: str, schema: dict) -> dict:
    """Best-effort map of alias -> 'schema.table' key, scanning FROM/JOIN clauses."""
    alias_map = {}
    table_keys_lower = {k.lower(): k for k in schema}
    bare_name_lookup = {}
    for key in schema:
        bare = key.split(".")[-1].lower()
        bare_name_lookup.setdefault(bare, []).append(key)

    for clause_match in re.finditer(
        r"(FROM|JOIN)\s+([^\n;]+?)(?=\bFROM\b|\bJOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bON\b|$)",
        full_text, re.IGNORECASE,
    ):
        clause = clause_match.group(2)
        for item in clause.split(","):
            item = item.strip()
            m = re.match(
                r"([A-Za-z0-9_\.\[\]]+)\s*(?:(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?$",
                item, re.IGNORECASE,
            )
            if not m:
                continue
            raw_name, alias = m.group(1), m.group(2)
            name = _strip_brackets(raw_name)
            key = table_keys_lower.get(name.lower())
            if key is None:
                candidates = bare_name_lookup.get(name.split(".")[-1].lower())
                if candidates and len(candidates) == 1:
                    key = candidates[0]
            if key is None:
                continue
            alias_map[name.lower()] = key
            if alias:
                alias_map[alias.lower()] = key
    return alias_map


def get_suggestions(full_text: str, cursor_index: int, schema: dict):
    """
    Returns (prefix_start_index, prefix, suggestions) where suggestions is a
    list of strings ranked best-first. prefix_start_index is the index in
    full_text where the current partial word begins (for replacement).
    """
    before = full_text[:cursor_index]
    word = _current_word(before)
    prefix_start = cursor_index - len(word)

    if "." in word:
        alias_part, col_prefix = word.rsplit(".", 1)
        alias_map = build_alias_map(full_text, schema)
        table_key = alias_map.get(_strip_brackets(alias_part).lower())
        if table_key is None:
            table_key = next(
                (k for k in schema if k.split(".")[-1].lower() == _strip_brackets(alias_part).lower()),
                None,
            )
        cols = [c[0] for c in schema.get(table_key, [])] if table_key else []
        suggestions = _rank(cols, col_prefix) if col_prefix else sorted(cols, key=str.lower)
        return cursor_index - len(col_prefix), col_prefix, suggestions

    if _TABLE_CONTEXT_RE.search(before):
        bare_names = sorted({k.split(".")[-1] for k in schema})
        full_names = sorted(schema.keys())
        pool = full_names + bare_names
        suggestions = _rank(pool, word) if word else sorted(set(pool), key=str.lower)[:50]
        return prefix_start, word, suggestions

    all_columns = sorted({c[0] for cols in schema.values() for c in cols})
    pool = SQL_KEYWORDS + sorted(schema.keys()) + all_columns
    suggestions = _rank(pool, word)
    return prefix_start, word, suggestions


def _rank(pool, prefix: str):
    if not prefix:
        return []
    prefix_l = prefix.lower()
    starts = sorted({p for p in pool if p.lower().startswith(prefix_l)}, key=str.lower)
    contains = sorted(
        {p for p in pool if prefix_l in p.lower() and not p.lower().startswith(prefix_l)},
        key=str.lower,
    )
    return (starts + contains)[:50]
