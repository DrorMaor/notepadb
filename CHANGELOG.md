# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-18

Initial public release.

### Added

- Multi-engine support: SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, SQLite.
- Schema-aware SQL autocomplete against the connected server.
- Independent tabs, each with its own connection, schema browser, and results grid.
- Results grid with per-cell coloring, CSV export, and schema-browser right-click
  commands (select top 1000 rows, script `CREATE TABLE`, copy column list, export
  table to CSV).
- Query tools: run, cancel, explain plan, SQL formatting, searchable history.
- Editor basics: find/replace, line numbers, word wrap, dark mode, session restore.
