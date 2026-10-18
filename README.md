# notepaDB

A notepad/scratchpad, but only for databases.

Type SQL, get autocomplete for real table and column names from the server you're
connected to, run it, see results. No project setup, no saved connections you have
to manage in some other tool — just open it and start querying, the way you'd open
Notepad for text.

Tabs are independent sessions (like SSMS query windows): each tab has its own
connection, schema browser, and results grid.

## Features

- **Multi-engine**: SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, and SQLite,
  all behind one consistent interface.
- **Schema-aware autocomplete** for real table and column names pulled live from
  the connected server — not just generic SQL keywords.
- **Independent tabs**, each with its own connection, schema browser, and results
  grid, similar to query windows in SSMS.
- **Results grid** with per-cell coloring (e.g. gray NULLs), CSV export, and a
  schema-browser right-click menu (select top 1000 rows, script `CREATE TABLE`,
  copy column list, export table to CSV).
- **Query tools**: run (F5), cancel, explain plan, SQL formatting, and searchable
  query history.
- **Editor basics**: find/replace, line numbers, word wrap, dark mode, and
  session restore across restarts.

## Installation

**Windows:** download the latest `notepadb.exe` from
[Releases](https://github.com/DrorMaor/notepadb/releases) and run it — no
Python install needed.

**From source** (any OS, or if you want to modify the code):

```bash
git clone https://github.com/DrorMaor/notepadb.git
cd notepadb
pip install -r requirements.txt
python -m notepadb
```

Or install it properly via pip:

```bash
pip install notepadb
notepadb
```

Note: `requirements.txt` installs drivers for every supported engine. If you
only need one or two (e.g. PostgreSQL and SQLite), you can skip the others —
each backend only fails at *connect time* if its driver is missing, not at
startup.

## Usage

Open a tab, pick **Connection → Connect...** (`Ctrl+K`), choose your engine,
and start typing SQL. Run with `F5`.

## Supported databases

| Engine | Driver |
|---|---|
| SQL Server | `pyodbc` |
| PostgreSQL | `psycopg2-binary` |
| MySQL / MariaDB | `mysql-connector-python` |
| Oracle | `oracledb` (thin mode — no Instant Client needed) |
| SQLite | stdlib `sqlite3` |

## Contributing

Bug reports, feature requests, and pull requests are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-2.0-only. See [LICENSE](LICENSE).

## Support

If notepaDB is useful to you, consider [buying me a coffee](https://ko-fi.com/drormaor).
