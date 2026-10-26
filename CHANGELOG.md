# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-27

### Added

- A native-feeling look on every platform: light and dark palettes derived from
  GNOME's Adwaita tokens, "follow system" theme detection, UI/monospace fonts
  that follow the desktop's own settings, HiDPI-aware sizing, and icons drawn
  at runtime (no image files).
- Command palette (`Ctrl+Shift+P`) over every command and the connected
  database's tables; keyboard-shortcuts reference (`F1`).
- Inline find/replace bar with live match counts (`Ctrl+F` / `Ctrl+H`).
- Editor commands: run the statement at the caret (`Ctrl+Enter`), run
  everything (`Ctrl+Shift+Enter`), toggle comment (`Ctrl+/`), move lines
  (`Alt+↑/↓`), indent/outdent a selection (`Tab`/`Shift+Tab`), go to line
  (`Ctrl+G`), zoom (`Ctrl+=`/`Ctrl+-`/`Ctrl+0`), tab switching (`Ctrl+Tab`),
  current-line highlight, right-click menu.
- Status bar (message, caret position, query time, connection badge), results
  footer (rows · columns · time · Export CSV), empty-state panels, tooltips,
  tab close buttons, middle-click close, tab context menu.
- Connect dialog with a saved-connections pane and a "Test connection" button;
  history dialog with a filter box and syntax-highlighted preview.
- Native GTK file chooser (via zenity) on Linux desktops that have it.
- Window geometry, sidebar width and editor zoom persist across restarts.

### Changed

- Query errors go to the Messages tab and the status bar instead of a modal
  dialog; Messages is now a tab next to the result sets.
- All stock Tk message boxes replaced by themed dialogs ("Save changes?" now
  offers Save / Discard / Cancel).
- Tk's Emacs-style text bindings no longer fire on `Ctrl+O`/`Ctrl+H`/`Ctrl+K`/
  `Ctrl+T` in the editor.
- Options menu merged into View (theme, line numbers, word wrap, sidebar).

### Fixed

- Autocomplete: aliases declared on their own line (`FROM teams t`) are now
  recognised, and table suggestions no longer vanish while typing `FROM te`.
- Ko-fi link in the About dialog opened without a scheme.

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
