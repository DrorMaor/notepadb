# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""
notepaDB — a notepad/scratchpad, but only for databases.

Type SQL, get autocomplete for real table/column names from the server you're
connected to, run it, see results. SQL Server, PostgreSQL, MySQL/MariaDB,
Oracle, and SQLite are all supported via db_backends.py.

Tabs are independent sessions (like SSMS query windows): each tab has its own
connection, schema browser, and results grid.

The look of the app (palettes, fonts, icons, ttk theme) lives in theme.py and
the reusable widgets (dialogs, find bar, command palette, status bar) in ui.py;
this module wires them to the actual database workflow.

Run with:  python -m notepadb
"""

__version__ = "0.1.0"

import csv
import glob
import json
import os
import sys
import webbrowser
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk

from tksheet import Sheet
from platformdirs import user_data_dir

from .db_backends import BACKENDS, BackendError
from .autocomplete import get_suggestions, SQL_KEYWORDS
from . import theme as T
from . import ui

APP_NAME = "notepaDB"
WEBSITE_URL = "https://notepadb.com"
GITHUB_URL = "https://github.com/DrorMaor/notepadb"
ISSUES_URL = "https://github.com/DrorMaor/notepadb/issues"
KOFI_URL = "https://ko-fi.com/drormaor"


def _app_version():
    try:
        from importlib.metadata import version
        return version("notepadb")
    except Exception:
        return __version__


APP_VERSION = _app_version()

# When frozen by PyInstaller, __file__ points into a temp extraction dir that
# changes every launch (onefile mode), so config saved there would never
# persist. Use the .exe's own directory instead in that case (lets a portable
# exe keep its data alongside itself). Otherwise — pip install or source
# checkout — site-packages isn't writable/appropriate for user data, so use
# the OS's per-user app-data directory.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = user_data_dir("notepadb", appauthor=False)
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, ".notepadb_config.json")
HISTORY_PATH = os.path.join(APP_DIR, ".notepadb_history.json")
HISTORY_LIMIT = 300
SESSION_PATH = os.path.join(APP_DIR, ".notepadb_session.json")
AUTOSAVE_DIR = os.path.join(APP_DIR, ".notepadb_autosave")
RESULT_ROW_CAP = 2000

KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(sorted({kw.split()[0] for kw in SQL_KEYWORDS}, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
STRING_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'")
LINE_COMMENT_PATTERN = re.compile(r"--.*")
BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
NUMBER_PATTERN = re.compile(r"\b\d+(\.\d+)?\b")
BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
BRACKET_PAIRS_REVERSE = {v: k for k, v in BRACKET_PAIRS.items()}


def _find_bracket_match(text, idx, open_ch, close_ch, forward):
    """Depth-counting scan for `idx`'s partner bracket. Doesn't special-case
    brackets inside string literals/comments — a minor cosmetic miss, not a
    correctness issue, since this only drives a highlight, nothing runs."""
    depth = 0
    if forward:
        for i in range(idx, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return i
    else:
        for i in range(idx, -1, -1):
            if text[i] == close_ch:
                depth += 1
            elif text[i] == open_ch:
                depth -= 1
                if depth == 0:
                    return i
    return None


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def update_config(**values):
    cfg = load_config()
    cfg.update(values)
    save_config(cfg)


def load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(history):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[:HISTORY_LIMIT], f, indent=2)
    except Exception:
        pass


def load_session():
    try:
        with open(SESSION_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_session(tabs_state):
    try:
        with open(SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(tabs_state, f, indent=2)
    except Exception:
        pass


def _autosave_path(tab_index):
    return os.path.join(AUTOSAVE_DIR, f"tab_{tab_index}.sql")


def _list_autosave_files():
    try:
        return sorted(glob.glob(os.path.join(AUTOSAVE_DIR, "tab_*.sql")))
    except Exception:
        return []


def record_history(sql, elapsed_ms, rows, connection_label):
    history = load_history()
    history.insert(0, {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sql": sql,
        "elapsed_ms": round(elapsed_ms),
        "rows": rows,
        "connection": connection_label,
    })
    save_history(history)


def statement_spans(sql):
    """Character spans [(start, end), ...] of the statements in a batch,
    splitting on ';' while skipping string literals and comments so a
    semicolon inside a quoted string or a comment doesn't split a statement.
    Spans are trimmed of surrounding whitespace; empty statements are dropped."""
    spans = []
    i, n = 0, len(sql)
    start = 0
    while i < n:
        ch = sql[i]
        if ch == "'":
            m = STRING_PATTERN.match(sql, i)
            if m:
                i = m.end()
                continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i)
            i = n if end == -1 else end + 2
            continue
        if ch == ";":
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, n))
    trimmed = []
    for s, e in spans:
        chunk = sql[s:e]
        if not chunk.strip():
            continue
        lead = len(chunk) - len(chunk.lstrip())
        trail = len(chunk) - len(chunk.rstrip())
        trimmed.append((s + lead, e - trail))
    return trimmed


def split_statements(sql):
    return [sql[s:e] for s, e in statement_spans(sql)]


def statement_at(sql, offset):
    """The statement under the caret: the span containing `offset`, else the
    nearest one before it when only whitespace separates them, else the next."""
    spans = statement_spans(sql)
    if not spans:
        return None
    for s, e in spans:
        if s <= offset <= e:
            return sql[s:e]
    before = [(s, e) for s, e in spans if e < offset]
    if before:
        s, e = before[-1]
        gap = sql[e:offset]
        if gap.count("\n") <= 1:
            return sql[s:e]
    after = [(s, e) for s, e in spans if s > offset]
    if after:
        s, e = after[0]
        return sql[s:e]
    s, e = before[-1]
    return sql[s:e]


def _sql_snippet(sql, limit=80):
    snippet = " ".join(sql.split())
    return snippet if len(snippet) <= limit else snippet[:limit - 1] + "…"


def _fmt_int(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_elapsed(ms):
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60000:
        return f"{ms / 1000:.2f} s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{int(minutes)} min {seconds:.0f} s"


_PLURALS = {"entry": "entries", "query": "queries"}


def _plural(n, word):
    if n == 1:
        return f"{_fmt_int(n)} {word}"
    return f"{_fmt_int(n)} {_PLURALS.get(word, word + 's')}"


def _quote_ident(name):
    return f'"{name}"'


ENGINE_DISPLAY_NAMES = {
    "sqlserver": "SQL Server",
    "postgres": "PostgreSQL",
    "mysql": "MySQL / MariaDB",
    "oracle": "Oracle",
    "sqlite": "SQLite",
}

# How each engine authenticates: (label for auth_mode="windows", label for
# auth_mode="sql", whether the choice is offered at all). Oracle always needs
# a username/password; SQLite needs nothing.
ENGINE_AUTH = {
    "sqlserver": ("Windows authentication", "SQL Server login", True),
    "postgres": ("No password (peer / trust)", "Username and password", True),
    "mysql": ("No password", "Username and password", True),
    "oracle": ("Username and password", "Username and password", False),
    "sqlite": ("", "", False),
}


def _quote_table_key(table_key):
    """`table_key` is whatever list_schema() used as a key — 'schema.table'
    or a bare table name — quote whichever parts it has."""
    if "." in table_key:
        schema, table = table_key.split(".", 1)
        return f"{_quote_ident(schema)}.{_quote_ident(table)}"
    return _quote_ident(table_key)


# Encodings to try in order when loading a file of unknown origin. cp1255 is
# the Windows "ANSI" codepage on Hebrew-locale machines, so .sql files saved
# by Notepad/SSMS there often aren't valid UTF-8 (e.g. Hebrew text like
# "לסניצקי" raises UnicodeDecodeError under strict utf-8).
FALLBACK_ENCODINGS = ["utf-8-sig", "utf-8", "cp1255", "cp1252"]


def read_text_file(path):
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _with_recent(history, value, limit=10):
    value = value.strip()
    if not value:
        return history
    return [value] + [v for v in history if v != value][: limit - 1]


SQL_FILETYPES = [("SQL files", "*.sql"), ("All files", "*.*")]
CSV_FILETYPES = [("CSV files", "*.csv"), ("All files", "*.*")]
SQLITE_FILETYPES = [("SQLite database", "*.db *.sqlite *.sqlite3 *.db3"), ("All files", "*.*")]


def highlight_sql_text(text_widget, start="1.0", end="end-1c"):
    """Apply the kw/str/comment/num tags to a Text widget's content."""
    text = text_widget.get(start, end)
    for tag in ("kw", "str", "comment", "num"):
        text_widget.tag_remove(tag, start, end)

    def mark(pattern, tag):
        for m in pattern.finditer(text):
            s, e = m.span()
            text_widget.tag_add(tag, f"{start}+{s}c", f"{start}+{e}c")

    mark(KEYWORD_PATTERN, "kw")
    mark(NUMBER_PATTERN, "num")
    mark(STRING_PATTERN, "str")
    mark(LINE_COMMENT_PATTERN, "comment")
    mark(BLOCK_COMMENT_PATTERN, "comment")


class Autocomplete:
    """Floating suggestion popup for the SQL editor. `owner` is a QueryTab —
    this only needs owner.schema / owner.editor / owner.highlight_syntax().
    Each row shows the completion plus what it is (table / column / keyword)."""

    MAX_ROWS = 10

    def __init__(self, owner):
        self.app = owner
        self.popup = None
        self.listbox = None
        self.prefix_start = None
        self._values = []
        self._kinds = []
        self._index_key = None
        self._index = (set(), set())

    @property
    def theme(self):
        return self.app.app.theme

    def update_suggestions(self):
        if self.app.schema is None:
            self.hide()
            return
        text_widget = self.app.editor
        full_text = text_widget.get("1.0", "end-1c")
        cursor_offset = len(text_widget.get("1.0", tk.INSERT))
        prefix_start, _prefix, suggestions = get_suggestions(full_text, cursor_offset, self.app.schema)
        if not suggestions:
            self.hide()
            return
        self.prefix_start = prefix_start
        self._show(suggestions)

    def _ensure_popup(self):
        if self.popup is not None:
            return
        self.popup = tk.Toplevel(self.app.editor)
        self.popup.wm_overrideredirect(True)
        try:
            self.popup.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.listbox = tk.Listbox(self.popup, exportselection=False, height=self.MAX_ROWS)
        self.listbox.pack(padx=1, pady=1)
        self.listbox.bind("<Double-Button-1>", self.accept)
        self.listbox.bind("<ButtonRelease-1>", lambda e: self.app.editor.focus_set())
        self.restyle()

    def restyle(self):
        if self.popup is None:
            return
        th = self.theme
        pal = th.pal
        self.popup.configure(background=pal.border)
        th.style_listbox(self.listbox, mono=True, bg=pal.popover)
        self.listbox.configure(selectbackground=pal.accent_soft, selectforeground=pal.fg)
        self._colorize()

    def _kind_index(self):
        schema = self.app.schema or {}
        key = (id(schema), len(schema))
        if key != self._index_key:
            tables = set()
            columns = set()
            for table_key, cols in schema.items():
                tables.add(table_key.lower())
                tables.add(table_key.split(".")[-1].lower())
                for col, _type in cols:
                    columns.add(col.lower())
            self._index = (tables, columns)
            self._index_key = key
        return self._index

    def _colorize(self):
        if self.listbox is None:
            return
        pal = self.theme.pal
        colors = {"table": pal.accent_text, "column": pal.fg, "keyword": pal.syn_kw}
        for i, kind in enumerate(self._kinds):
            try:
                self.listbox.itemconfig(i, foreground=colors[kind])
            except tk.TclError:
                pass

    def _show(self, suggestions):
        self._ensure_popup()
        tables, columns = self._kind_index()
        self._values = list(suggestions[:50])
        width = max(len(v) for v in self._values)
        self.listbox.delete(0, tk.END)
        self._kinds = []
        for v in self._values:
            low = v.lower()
            kind = "table" if low in tables else ("column" if low in columns else "keyword")
            self._kinds.append(kind)
            self.listbox.insert(tk.END, f" {v.ljust(width + 3)}{kind}")
        self._colorize()
        self.listbox.configure(height=min(self.MAX_ROWS, len(self._values)), width=width + 3 + 7 + 2)
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(0)
        self.listbox.see(0)

        text_widget = self.app.editor
        bbox = text_widget.bbox(tk.INSERT)
        if bbox:
            x, y, _w, h = bbox
            gap = self.theme.px(4)
            self.popup.update_idletasks()
            pw, ph = self.popup.winfo_reqwidth(), self.popup.winfo_reqheight()
            abs_x = text_widget.winfo_rootx() + x
            abs_y = text_widget.winfo_rooty() + y + h + gap
            sw, sh = text_widget.winfo_screenwidth(), text_widget.winfo_screenheight()
            if abs_y + ph > sh:
                abs_y = text_widget.winfo_rooty() + y - ph - gap
            if abs_x + pw > sw:
                abs_x = max(0, sw - pw)
            self.popup.geometry(f"+{abs_x}+{abs_y}")
        self.popup.deiconify()
        self.popup.lift()

    def hide(self):
        if self.popup is not None:
            self.popup.withdraw()

    def visible(self):
        return self.popup is not None and self.popup.winfo_viewable()

    def accept(self, event=None):
        if not self.visible():
            return None
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self._values):
            self.hide()
            return None
        value = self._values[sel[0]]
        text_widget = self.app.editor
        start_index = f"1.0+{self.prefix_start}c"
        text_widget.delete(start_index, tk.INSERT)
        text_widget.insert(start_index, value)
        self.hide()
        text_widget.focus_set()
        self.app.highlight_syntax()
        return "break"

    def move(self, delta):
        if not self.visible():
            return False
        size = self.listbox.size()
        if size == 0:
            return False
        cur = self.listbox.curselection()
        idx = cur[0] if cur else 0
        idx = (idx + delta) % size
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        return True


class ConnectDialog(ui.Dialog):
    """Two-pane connection dialog: saved connections on the left, the form
    on the right, Test / Cancel / Connect along the bottom."""

    def __init__(self, parent, defaults):
        super().__init__(parent, "Connect to a database", padding=0)
        self.defaults = defaults
        self._saved_profiles = dict(defaults.get("saved_connections", {}))
        self._test_queue = queue.Queue()
        self._testing = False
        self._build(defaults)

    # ---------- construction ----------

    def _build(self, defaults):
        th = ui.current()
        px = th.px
        self.buttons.configure(padding=(px(18), px(12), px(18), px(16)))

        body = self.body
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- left: saved connections ----
        left = ttk.Frame(body, style="Sidebar.TFrame", padding=(px(16), px(16), px(16), px(16)))
        left.grid(row=0, column=0, sticky="nsw")
        ttk.Label(left, text="Saved connections", style="SidebarHeading.TLabel").pack(anchor="w", pady=(0, px(8)))
        list_card = ttk.Frame(left, style="Card.TFrame", padding=1)
        list_card.pack(fill="both", expand=True)
        self.saved_list = tk.Listbox(list_card, exportselection=False, height=11, width=24)
        th.style_listbox(self.saved_list)
        self.saved_list.pack(side="left", fill="both", expand=True)
        self.saved_list.bind("<<ListboxSelect>>", self._on_saved_pick)
        self.saved_list.bind("<Double-Button-1>", lambda e: self._connect())
        self.saved_empty = tk.Label(
            list_card, text="Nothing saved yet.\nFill in the form, then\nclick “Save as…”.",
            background=th.pal.view, foreground=th.pal.fg_faint, font=th.fonts.small.name, justify="center",
        )
        row = ttk.Frame(left, style="Sidebar.TFrame")
        row.pack(fill="x", pady=(px(8), 0))
        self.save_btn = ttk.Button(row, text="Save as…", image=th.icon("plus"), compound="left", style="Sidebar.Toolbutton",
                                   command=self._save_connection_profile)
        self.save_btn.pack(side="left")
        self.delete_btn = ttk.Button(row, text="Delete", style="Sidebar.Toolbutton", command=self._delete_connection_profile)
        self.delete_btn.pack(side="left", padx=(px(4), 0))
        ttk.Separator(body, orient="vertical").grid(row=0, column=0, sticky="nse")

        # ---- right: form ----
        form = ttk.Frame(body, style="Dialog.TFrame", padding=(px(22), px(18), px(22), px(10)))
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)
        self.form = form
        pady = (0, px(10))
        row_i = 0

        def label(text, r):
            lbl = ttk.Label(form, text=text, style="Dialog.TLabel", anchor="e")
            lbl.grid(row=r, column=0, sticky="e", padx=(0, px(12)), pady=pady)
            return lbl

        label("Engine", row_i)
        self.engine_var = tk.StringVar(value=defaults.get("engine", "sqlserver"))
        self._engine_display_var = tk.StringVar()
        self._engine_display_map = {v: k for k, v in ENGINE_DISPLAY_NAMES.items()}
        self.engine_box = ttk.Combobox(form, textvariable=self._engine_display_var, state="readonly",
                                       values=list(self._engine_display_map.keys()))
        self.engine_box.grid(row=row_i, column=1, sticky="we", pady=pady)
        self._engine_display_var.set(ENGINE_DISPLAY_NAMES.get(self.engine_var.get(), "SQL Server"))
        self.engine_box.bind("<<ComboboxSelected>>", self._on_engine_pick)
        row_i += 1

        # SQLite: file path + browse
        self.file_label = label("Database file", row_i)
        file_row = ttk.Frame(form, style="Dialog.TFrame")
        file_row.grid(row=row_i, column=1, sticky="we", pady=pady)
        file_row.columnconfigure(0, weight=1)
        self.server_var = tk.StringVar(value=defaults.get("server", ""))
        self.file_entry = ttk.Entry(file_row, textvariable=self.server_var)
        self.file_entry.grid(row=0, column=0, sticky="we")
        self.browse_btn = ttk.Button(file_row, text="Browse…", command=self._browse_file)
        self.browse_btn.grid(row=0, column=1, padx=(px(8), 0))
        self.file_row = file_row
        row_i += 1

        self.server_label = label("Server", row_i)
        self.server_box = ttk.Combobox(form, textvariable=self.server_var, values=defaults.get("recent_servers", []))
        self.server_box.grid(row=row_i, column=1, sticky="we", pady=pady)
        row_i += 1

        self.port_label = label("Port", row_i)
        port_row = ttk.Frame(form, style="Dialog.TFrame")
        port_row.grid(row=row_i, column=1, sticky="we", pady=pady)
        self.port_var = tk.StringVar(value=defaults.get("port") or "")
        self.port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=9)
        self.port_entry.pack(side="left")
        self.port_hint = ttk.Label(port_row, text="Leave empty for the default", style="DialogMuted.TLabel")
        self.port_hint.pack(side="left", padx=(px(10), 0))
        self.port_row = port_row
        row_i += 1

        self.database_label = label("Database", row_i)
        self.database_var = tk.StringVar(value=defaults.get("database") or "")
        self.database_box = ttk.Combobox(form, textvariable=self.database_var, values=defaults.get("recent_databases", []))
        self.database_box.grid(row=row_i, column=1, sticky="we", pady=pady)
        row_i += 1

        self.auth_label = label("Sign in", row_i)
        self.auth_var = tk.StringVar(value=defaults.get("auth_mode", "windows"))
        self.auth_frame = ttk.Frame(form, style="Dialog.TFrame")
        self.auth_frame.grid(row=row_i, column=1, sticky="we", pady=pady)
        self.auth_radio_a = ttk.Radiobutton(self.auth_frame, text="Windows authentication", variable=self.auth_var,
                                            value="windows", command=self._sync_auth_fields, style="Dialog.TRadiobutton")
        self.auth_radio_a.pack(side="left")
        self.auth_radio_b = ttk.Radiobutton(self.auth_frame, text="SQL Server login", variable=self.auth_var,
                                            value="sql", command=self._sync_auth_fields, style="Dialog.TRadiobutton")
        self.auth_radio_b.pack(side="left", padx=(px(14), 0))
        row_i += 1

        self.username_label = label("Username", row_i)
        self.username_var = tk.StringVar(value=defaults.get("username", ""))
        self.username_entry = ttk.Entry(form, textvariable=self.username_var)
        self.username_entry.grid(row=row_i, column=1, sticky="we", pady=pady)
        row_i += 1

        self.password_label = label("Password", row_i)
        self.password_var = tk.StringVar(value="")
        self.password_entry = ttk.Entry(form, textvariable=self.password_var, show="•")
        self.password_entry.grid(row=row_i, column=1, sticky="we", pady=pady)
        row_i += 1

        self.opts_frame = ttk.Frame(form, style="Dialog.TFrame")
        self.opts_frame.grid(row=row_i, column=1, sticky="we", pady=pady)
        self.encrypt_var = tk.BooleanVar(value=defaults.get("encrypt", True))
        self.trust_var = tk.BooleanVar(value=defaults.get("trust_cert", True))
        ttk.Checkbutton(self.opts_frame, text="Encrypt connection (TLS)", variable=self.encrypt_var,
                        style="Dialog.TCheckbutton").pack(side="left")
        ttk.Checkbutton(self.opts_frame, text="Trust server certificate", variable=self.trust_var,
                        style="Dialog.TCheckbutton").pack(side="left", padx=(px(14), 0))
        row_i += 1

        status_row = ttk.Frame(form, style="Dialog.TFrame")
        status_row.grid(row=row_i, column=0, columnspan=2, sticky="we", pady=(px(4), 0))
        self.status_icon = ttk.Label(status_row, style="Dialog.TLabel")
        self.status_label = ttk.Label(status_row, text="", style="DialogMuted.TLabel", wraplength=px(420), justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        row_i += 1
        form.rowconfigure(row_i, weight=1)

        # ---- buttons ----
        self.test_btn = self.add_button("Test connection", self._test_connection, side="left")
        self.connect_btn = self.add_button("Connect", self._connect, kind="accent", default=True)
        self.cancel_btn = self.add_button("Cancel", self.cancel)

        self._refresh_saved_list()
        self._sync_engine_fields()
        self.after_idle(self._initial_focus)

    def _initial_focus(self):
        target = self.file_entry if self.engine_var.get() == "sqlite" else self.server_box
        if not self.server_var.get().strip():
            target.focus_set()
        else:
            self.connect_btn.focus_set()

    # ---------- field visibility ----------

    def _on_engine_pick(self, event=None):
        display = self._engine_display_var.get()
        self.engine_var.set(self._engine_display_map.get(display, "sqlserver"))
        self._sync_engine_fields()

    def _sync_auth_fields(self):
        engine = self.engine_var.get()
        _a, _b, offered = ENGINE_AUTH.get(engine, ENGINE_AUTH["sqlserver"])
        needs_credentials = (not offered) or self.auth_var.get() == "sql"
        state = "normal" if needs_credentials else "disabled"
        self.username_entry.configure(state=state)
        self.password_entry.configure(state=state)
        if engine in ("postgres", "mysql") and self.auth_var.get() != "sql":
            # These engines still accept a user name without a password.
            self.username_entry.configure(state="normal")

    def _sync_engine_fields(self):
        engine = self.engine_var.get()
        is_sqlite = engine == "sqlite"
        label_a, label_b, offered = ENGINE_AUTH.get(engine, ENGINE_AUTH["sqlserver"])
        sqlite_widgets = [self.file_label, self.file_row]
        server_widgets = [
            self.server_label, self.server_box, self.port_label, self.port_row,
            self.database_label, self.database_box, self.username_label, self.username_entry,
            self.password_label, self.password_entry,
        ]
        auth_widgets = [self.auth_label, self.auth_frame]
        tls_widgets = [self.opts_frame]
        for w in sqlite_widgets:
            w.grid() if is_sqlite else w.grid_remove()
        for w in server_widgets:
            w.grid_remove() if is_sqlite else w.grid()
        for w in auth_widgets:
            w.grid() if (offered and not is_sqlite) else w.grid_remove()
        # Every networked backend honours Encrypt / Trust certificate (sslmode,
        # ssl_disabled, tcps...), so the options stay available for all of them.
        for w in tls_widgets:
            w.grid_remove() if is_sqlite else w.grid()
        if offered:
            self.auth_radio_a.configure(text=label_a)
            self.auth_radio_b.configure(text=label_b)
        self.database_label.configure(text="Service name" if engine == "oracle" else "Database")
        if not is_sqlite:
            self._sync_auth_fields()

    # ---------- saved connections ----------

    def _refresh_saved_list(self, select=None):
        self.saved_list.delete(0, "end")
        names = sorted(self._saved_profiles.keys(), key=str.lower)
        for name in names:
            self.saved_list.insert("end", f"  {name}")
        if names:
            self.saved_empty.place_forget()
        else:
            self.saved_empty.place(relx=0.5, rely=0.45, anchor="center")
        if select in names:
            idx = names.index(select)
            self.saved_list.selection_clear(0, "end")
            self.saved_list.selection_set(idx)
            self.saved_list.see(idx)
        self.delete_btn.configure(state="normal" if self.saved_list.curselection() else "disabled")

    def _selected_profile_name(self):
        sel = self.saved_list.curselection()
        if not sel:
            return None
        return self.saved_list.get(sel[0]).strip()

    def _on_saved_pick(self, event=None):
        name = self._selected_profile_name()
        self.delete_btn.configure(state="normal" if name else "disabled")
        profile = self._saved_profiles.get(name) if name else None
        if not profile:
            return
        self.engine_var.set(profile.get("engine", "sqlserver"))
        self._engine_display_var.set(ENGINE_DISPLAY_NAMES.get(self.engine_var.get(), "SQL Server"))
        self.server_var.set(profile.get("server", ""))
        self.port_var.set(profile.get("port") or "")
        self.database_var.set(profile.get("database") or "")
        self.auth_var.set(profile.get("auth_mode", "windows"))
        self.username_var.set(profile.get("username", ""))
        self.encrypt_var.set(profile.get("encrypt", True))
        self.trust_var.set(profile.get("trust_cert", True))
        self.password_var.set("")
        self._sync_engine_fields()
        self._set_status(f"Loaded “{name}”." + ("" if self.engine_var.get() == "sqlite" or self.auth_var.get() != "sql"
                                                 else " Enter the password to connect."), "muted")

    def _current_profile(self):
        return {
            "engine": self.engine_var.get(),
            "server": self.server_var.get().strip(),
            "port": self.port_var.get().strip() or None,
            "database": self.database_var.get().strip() or None,
            "auth_mode": self.auth_var.get(),
            "username": self.username_var.get().strip(),
            "encrypt": self.encrypt_var.get(),
            "trust_cert": self.trust_var.get(),
        }

    def _save_connection_profile(self):
        suggestion = self._selected_profile_name() or ""
        if not suggestion:
            server = self.server_var.get().strip()
            if self.engine_var.get() == "sqlite":
                suggestion = os.path.splitext(os.path.basename(server))[0] if server else ""
            else:
                db = self.database_var.get().strip()
                suggestion = f"{server}/{db}" if server and db else server
        name = ui.ask_string(self, "Save connection", "Name for this connection", initial=suggestion, ok="Save",
                             validate=lambda v: "Enter a name." if not v.strip() else None)
        if not name:
            return
        name = name.strip()
        cfg = load_config()
        profiles = cfg.setdefault("saved_connections", {})
        profiles[name] = self._current_profile()
        save_config(cfg)
        self._saved_profiles = profiles
        self._refresh_saved_list(select=name)
        self._set_status(f"Saved “{name}”.", "success")

    def _delete_connection_profile(self):
        name = self._selected_profile_name()
        if not name or name not in self._saved_profiles:
            return
        if not ui.ask_yes_no(self, "Delete saved connection?", f"“{name}” will be removed from your saved connections.",
                             yes="Delete", destructive=True):
            return
        cfg = load_config()
        profiles = cfg.get("saved_connections", {})
        profiles.pop(name, None)
        save_config(cfg)
        self._saved_profiles = profiles
        self._refresh_saved_list()
        self._set_status(f"Deleted “{name}”.", "muted")

    # ---------- helpers ----------

    def _set_status(self, text, kind="muted"):
        style = {"muted": "DialogMuted.TLabel", "error": "DialogError.TLabel", "success": "DialogSuccess.TLabel"}[kind]
        self.status_label.configure(text=text, style=style)

    def _browse_file(self):
        path = ui.pick_open_file(self, "Select a SQLite database file", SQLITE_FILETYPES)
        if path:
            self.server_var.set(path)

    def _params(self):
        engine = self.engine_var.get()
        if engine == "sqlite":
            file_path = self.server_var.get().strip()
            if not file_path:
                self._set_status("Choose the database file to open.", "error")
                self.file_entry.focus_set()
                return None
            return {
                "engine": engine, "server": file_path, "port": None,
                "database": os.path.basename(file_path), "auth_mode": "windows",
                "username": "", "password": "", "encrypt": True, "trust_cert": True,
            }
        if not self.server_var.get().strip():
            self._set_status("Server is required.", "error")
            self.server_box.focus_set()
            return None
        params = self._current_profile()
        params["password"] = self.password_var.get()
        return params

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.test_btn, self.connect_btn, self.save_btn):
            b.configure(state=state)
        self.configure(cursor="watch" if busy else "")

    def _test_connection(self):
        if self._testing:
            return
        params = self._params()
        if params is None:
            return
        self._testing = True
        self._set_busy(True)
        self._set_status("Testing connection…", "muted")
        started = time.monotonic()

        def work():
            try:
                backend = BACKENDS[params["engine"]]()
                backend.connect(
                    server=params["server"], database=params["database"], auth_mode=params["auth_mode"],
                    username=params["username"], password=params["password"], encrypt=params["encrypt"],
                    trust_cert=params["trust_cert"], port=params["port"],
                )
                try:
                    count = len(backend.list_databases())
                finally:
                    backend.disconnect()
                self._test_queue.put((True, count, (time.monotonic() - started) * 1000))
            except Exception as exc:  # BackendError or a driver import failure
                self._test_queue.put((False, str(exc), (time.monotonic() - started) * 1000))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll_test)

    def _poll_test(self):
        try:
            ok, payload, elapsed = self._test_queue.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(100, self._poll_test)
            return
        self._testing = False
        if not self.winfo_exists():
            return
        self._set_busy(False)
        if ok:
            self._set_status(f"Connected in {_fmt_elapsed(elapsed)} · {_plural(payload, 'database')} visible.", "success")
        else:
            self._set_status(payload, "error")

    def _connect(self):
        if self._testing:
            return
        params = self._params()
        if params is None:
            return
        self.close(params)


class HistoryDialog(ui.Dialog):
    """Non-modal browser over every successfully-run statement, newest
    first, with a filter box and a syntax-highlighted preview. Double-click
    (or Insert) drops it into the active tab's editor; Insert & Run also
    executes it immediately."""

    def __init__(self, app):
        super().__init__(app.root, "Query history", modal=False, resizable=(True, True))
        self.app = app
        self.history = load_history()
        self._visible = []
        self._build()
        px = app.theme.px
        self.minsize(px(720), px(420))
        w = min(px(940), self.winfo_screenwidth() - px(80))
        h = min(px(620), self.winfo_screenheight() - px(140))
        self.geometry(f"{w}x{h}")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self):
        th = self.app.theme
        px = th.px
        top = ttk.Frame(self.body, style="Dialog.TFrame")
        top.pack(fill="x", pady=(0, px(10)))
        ttk.Label(top, image=th.icon("search", "fg_muted"), style="Dialog.TLabel").pack(side="left", padx=(0, px(6)))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(top, textvariable=self.filter_var)
        self.filter_entry.pack(side="left", fill="x", expand=True)
        ui.Placeholder(self.filter_entry, "Filter by SQL text or connection", self.filter_var)
        self.filter_var.trace_add("write", lambda *a: self._populate())
        self.count_label = ttk.Label(top, text="", style="DialogMuted.TLabel")
        self.count_label.pack(side="left", padx=(px(10), 0))

        pane = ttk.PanedWindow(self.body, orient="vertical")
        pane.pack(fill="both", expand=True)

        table_card = ttk.Frame(pane, style="Card.TFrame", padding=1)
        pane.add(table_card, weight=3)
        columns = ("time", "elapsed", "rows", "connection", "sql")
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        headers = {
            "time": ("Time", 150, "w"), "elapsed": ("Duration", 80, "e"), "rows": ("Rows", 70, "e"),
            "connection": ("Connection", 180, "w"), "sql": ("SQL", 380, "w"),
        }
        for key, (text, width, anchor) in headers.items():
            self.tree.heading(key, text=text, anchor=anchor)
            self.tree.column(key, width=px(width), minwidth=px(50), anchor=anchor, stretch=(key == "sql"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ui.AutoScrollbar(table_card, orient="vertical", command=self.tree.yview, style="View.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_preview())
        self.tree.bind("<Double-Button-1>", lambda e: self._insert_selected())
        self.tree.bind("<Return>", lambda e: self._insert_selected())

        preview_card = ttk.Frame(pane, style="Card.TFrame", padding=1)
        pane.add(preview_card, weight=2)
        self.preview = tk.Text(preview_card, height=7, state="disabled")
        th.style_preview(self.preview)
        self.preview.configure(highlightthickness=0)
        self.preview.pack(side="left", fill="both", expand=True)
        pscroll = ui.AutoScrollbar(preview_card, orient="vertical", command=self.preview.yview, style="View.Vertical.TScrollbar")
        pscroll.pack(side="right", fill="y")
        self.preview.configure(yscrollcommand=pscroll.set)

        self.add_button("Clear history", self._clear, side="left")
        self.insert_run_btn = self.add_button("Insert & run", lambda: self._insert_selected(run=True), kind="accent", default=True)
        self.insert_btn = self.add_button("Insert", self._insert_selected)
        self.add_button("Close", self.destroy)
        self._populate()
        self.after_idle(self.filter_entry.focus_set)

    def refresh(self):
        self.history = load_history()
        self._populate()

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        query = self.filter_var.get().strip().lower()
        self._visible = []
        for entry in self.history:
            hay = f"{entry.get('sql', '')} {entry.get('connection', '')}".lower()
            if query and query not in hay:
                continue
            self._visible.append(entry)
            self.tree.insert("", "end", values=(
                entry.get("time", ""), _fmt_elapsed(entry.get("elapsed_ms")), _fmt_int(entry.get("rows", "")),
                entry.get("connection", ""), _sql_snippet(entry.get("sql", ""), limit=140),
            ))
        total = len(self.history)
        shown = len(self._visible)
        self.count_label.configure(text=(f"{_fmt_int(shown)} of {_fmt_int(total)}" if query else _plural(total, "entry")))
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
        self._update_preview()

    def _selected_entry(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = self.tree.index(sel[0])
        return self._visible[idx] if idx < len(self._visible) else None

    def _update_preview(self):
        entry = self._selected_entry()
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if entry is not None:
            self.preview.insert("1.0", entry.get("sql", ""))
            highlight_sql_text(self.preview)
        self.preview.configure(state="disabled")
        state = "normal" if entry is not None else "disabled"
        self.insert_btn.configure(state=state)
        self.insert_run_btn.configure(state=state)

    def _insert_selected(self, run=False):
        entry = self._selected_entry()
        if entry is None:
            return
        tab = self.app.active_tab
        if tab is None:
            return
        tab._insert_and_run(entry["sql"]) if run else tab._insert_text(entry["sql"])
        self.destroy()

    def _clear(self):
        if not ui.ask_yes_no(self, "Clear query history?", "Every saved statement will be removed. This cannot be undone.",
                             yes="Clear history", destructive=True):
            return
        save_history([])
        self.history = []
        self._populate()


SHORTCUT_GROUPS = [
    ("Query", [
        ("Run selection, or everything", "F5"),
        ("Run the statement at the caret", "Ctrl+Enter"),
        ("Run everything", "Ctrl+Shift+Enter"),
        ("Stop the running query", "Ctrl+Break"),
        ("Explain plan", "Ctrl+L"),
        ("Query history", "Ctrl+Shift+H"),
    ]),
    ("Connection", [
        ("Connect…", "Ctrl+K"),
        ("Refresh schema", "Ctrl+Shift+R"),
        ("Show / hide schema sidebar", "Ctrl+B"),
    ]),
    ("Editor", [
        ("Find", "Ctrl+F"),
        ("Find next / previous", "F3 / Shift+F3"),
        ("Replace", "Ctrl+H"),
        ("Toggle line comment", "Ctrl+/"),
        ("Format SQL", "Ctrl+Shift+F"),
        ("Go to line", "Ctrl+G"),
        ("Move line up / down", "Alt+↑ / Alt+↓"),
        ("Indent / outdent selection", "Tab / Shift+Tab"),
        ("Accept completion", "Tab or Enter"),
        ("Zoom in / out / reset", "Ctrl+= / Ctrl+- / Ctrl+0"),
    ]),
    ("Tabs & files", [
        ("New tab", "Ctrl+T"),
        ("Close tab", "Ctrl+W"),
        ("Next / previous tab", "Ctrl+Tab / Ctrl+Shift+Tab"),
        ("Open file", "Ctrl+O"),
        ("Save / Save as", "Ctrl+S / Ctrl+Shift+S"),
        ("Command palette", "Ctrl+Shift+P"),
        ("Full screen", "F11"),
        ("Keyboard shortcuts", "F1"),
        ("Quit", "Ctrl+Q"),
    ]),
]


class ShortcutsDialog(ui.Dialog):
    def __init__(self, parent, theme):
        super().__init__(parent, "Keyboard shortcuts")
        px = theme.px
        grid = ttk.Frame(self.body, style="Dialog.TFrame")
        grid.pack(fill="both", expand=True)
        for col, (title, items) in enumerate(SHORTCUT_GROUPS):
            r, c = divmod(col, 2)
            group = ttk.Frame(grid, style="Dialog.TFrame")
            group.grid(row=r, column=c, sticky="nw", padx=(0, px(40)) if c == 0 else 0, pady=(0, px(20)) if r == 0 else 0)
            ttk.Label(group, text=title, style="DialogHeading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(6)))
            for i, (action, keys) in enumerate(items, start=1):
                ttk.Label(group, text=action, style="Dialog.TLabel").grid(row=i, column=0, sticky="w", pady=px(2))
                ttk.Label(group, text=keys, style="DialogMuted.TLabel", font=theme.fonts.mono_small.name).grid(
                    row=i, column=1, sticky="e", padx=(px(24), 0), pady=px(2))
            group.columnconfigure(0, weight=1)
        self.add_button("Close", self.cancel, kind="accent", default=True)


class AboutDialog(ui.Dialog):
    def __init__(self, parent, theme):
        super().__init__(parent, f"About {APP_NAME}")
        px = theme.px
        row = ttk.Frame(self.body, style="Dialog.TFrame")
        row.pack(fill="both", expand=True)
        ttk.Label(row, image=theme.icon("logo", "accent", 72), style="Dialog.TLabel").pack(side="left", anchor="n", padx=(0, px(22)))
        col = ttk.Frame(row, style="Dialog.TFrame")
        col.pack(side="left", fill="both", expand=True)
        ttk.Label(col, text=APP_NAME, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(col, text="A notepad, but only for databases.", style="DialogMuted.TLabel").pack(anchor="w", pady=(px(2), px(12)))
        ttk.Label(col, text=(
            "Type SQL, autocomplete the real table and column names on your\n"
            "server, press F5. Every tab is its own session with its own\n"
            "connection, schema browser and results."
        ), style="Dialog.TLabel", justify="left").pack(anchor="w")
        ttk.Label(col, text="SQL Server · PostgreSQL · MySQL / MariaDB · Oracle · SQLite",
                  style="DialogMuted.TLabel").pack(anchor="w", pady=(px(10), 0))
        ttk.Label(col, text=f"Version {APP_VERSION} · Free software (GPL-2.0)", style="DialogMuted.TLabel").pack(anchor="w", pady=(px(2), px(12)))
        links = ttk.Frame(col, style="Dialog.TFrame")
        links.pack(anchor="w")
        for i, (text, url) in enumerate((("Website", WEBSITE_URL), ("Source code", GITHUB_URL), ("Buy me a coffee", KOFI_URL))):
            if i:
                ttk.Label(links, text="·", style="DialogMuted.TLabel").pack(side="left", padx=px(6))
            link = ttk.Label(links, text=text, style="Link.TLabel", cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        self.add_button("Close", self.cancel, kind="accent", default=True)


class QueryTab:
    """One independent session: own connection, schema browser, editor and
    results grid. Conceptually the same as one SSMS query window."""

    TITLE_PREFIX = "Query"
    DRAG_THRESHOLD_PX = 4

    def __init__(self, app, index):
        self.app = app
        self.index = index
        self.title = f"{self.TITLE_PREFIX} {index}"
        self.current_file = None
        self.dirty = False

        self.backend = None
        self.connected_server = None
        self._last_auth_mode = "windows"
        self._last_port = None
        self._last_encrypt = True
        self._last_trust_cert = True
        self.databases = []  # all database names on the connected server
        self.db_schema_cache = {}  # db_name -> {'schema.table': [(col, type), ...]}
        self.active_database = None  # db the connection is USE'd into, for Run/autocomplete
        self.schema = None  # active db's schema

        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.match_case_var = tk.BooleanVar(value=False)

        self._work_queue = queue.Queue()
        self._closed = False
        self._query_running = False
        self._cancel_requested = False
        self._run_started = None
        self.last_status = ("Ready.", "info")
        self.last_timing = ""
        self._filter_after = None
        self._sash_applied = False

        self._tree_search_buffer = ""
        self._tree_search_last_time = 0.0
        self._tooltip_window = None
        self._tooltip_after_id = None
        self._tooltip_item = None
        self._drag_data = None
        self._drag_ghost = None
        self._drag_ghost_label = None

        px = app.theme.px
        self.frame = ttk.Frame(app.notebook, padding=(px(10), px(8), px(10), px(8)))
        self._build_body()
        self.autocomplete = Autocomplete(self)
        self.apply_theme(self.app.theme.pal)
        self.apply_word_wrap(self.app.word_wrap_var.get())
        self.apply_line_numbers_visibility(self.app.show_line_numbers_var.get())
        self.apply_sidebar_visibility(self.app.show_sidebar_var.get())

        self._bind_editor_events()
        self.highlight_syntax()
        self._update_line_numbers()
        self._update_editor_hint()
        self._sync_sidebar_state()
        self._log_message("Ready.")
        self.root.after(100, self._poll_queue)

    @property
    def root(self):
        return self.app.root

    @property
    def theme(self):
        return self.app.theme

    # ---------- body construction ----------

    def _build_body(self):
        th = self.theme
        px = th.px

        self.main = ttk.PanedWindow(self.frame, orient="horizontal")
        self.main.pack(side="top", fill="both", expand=True)
        self.main.bind("<Map>", self._apply_initial_sash, add="+")

        # --- left: schema browser ---
        self.sidebar = ttk.Frame(self.main, style="SidebarCard.TFrame", padding=1, width=self.app.sidebar_width)
        self.sidebar.pack_propagate(False)
        header = ttk.Frame(self.sidebar, style="Sidebar.TFrame", padding=(px(12), px(6), px(4), px(2)))
        header.pack(side="top", fill="x")
        ttk.Label(header, text="Schema", style="SidebarHeading.TLabel").pack(side="left")
        self.refresh_btn = ttk.Button(header, image=th.icon("refresh", "fg_muted"), style="Sidebar.Icon.Toolbutton",
                                      command=self.refresh_schema)
        self.refresh_btn.pack(side="right")
        ui.Tooltip(self.refresh_btn, "Refresh schema (Ctrl+Shift+R)")

        filter_row = ttk.Frame(self.sidebar, style="Sidebar.TFrame", padding=(px(10), px(2), px(10), px(8)))
        filter_row.pack(side="top", fill="x")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var, style="Sidebar.TEntry")
        self.filter_entry.pack(fill="x")
        ui.Placeholder(self.filter_entry, "Filter tables and columns", self.filter_var)
        self.filter_var.trace_add("write", lambda *a: self._schedule_filter())
        self.filter_entry.bind("<Escape>", lambda e: (self.filter_var.set(""), self.editor.focus_set(), "break")[2])
        self.filter_entry.bind("<Return>", lambda e: (self.schema_tree.focus_set(), "break")[1])

        tree_frame = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        tree_frame.pack(side="top", fill="both", expand=True)
        self.schema_tree = ttk.Treeview(tree_frame, show="tree", style="Sidebar.Treeview", selectmode="browse")
        self.schema_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ui.AutoScrollbar(tree_frame, orient="vertical", command=self.schema_tree.yview, style="Sidebar.Vertical.TScrollbar")
        tree_scroll.pack(side="right", fill="y")
        self.schema_tree.configure(yscrollcommand=tree_scroll.set)
        self.schema_tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.schema_tree.bind("<Button-3>", self._on_tree_right_click)
        self.schema_tree.bind("<ButtonPress-1>", self._on_tree_press, add="+")
        self.schema_tree.bind("<B1-Motion>", self._on_tree_motion, add="+")
        self.schema_tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")
        self.schema_tree.bind("<Double-Button-1>", self._on_tree_double_click, add="+")
        self.schema_tree.bind("<KeyPress>", self._on_tree_keypress)
        self.schema_tree.bind("<Motion>", self._on_tree_hover, add="+")
        self.schema_tree.bind("<Leave>", self._on_tree_leave, add="+")

        self.sidebar_empty = ui.EmptyState(
            self.sidebar, "database", "Not connected",
            "Connect to browse the databases,\ntables and columns you can query.",
            action="Connect…", action_command=self.open_connect_dialog, style_bg="sidebar",
        )
        self.main.add(self.sidebar, weight=0)

        # --- right: editor over results ---
        self.right = ttk.PanedWindow(self.main, orient="vertical")
        self.main.add(self.right, weight=1)

        self.editor_card = ttk.Frame(self.right, style="Card.TFrame", padding=1)
        self.editor_area = ttk.Frame(self.editor_card, style="View.TFrame")
        self.editor_area.pack(side="top", fill="both", expand=True)
        self.editor_area.columnconfigure(1, weight=1)
        self.editor_area.rowconfigure(0, weight=1)
        self.line_numbers = tk.Text(self.editor_area, width=4)
        self.line_numbers.grid(row=0, column=0, sticky="ns")
        self.editor = tk.Text(self.editor_area, wrap="none", undo=True, maxundo=-1, autoseparators=True)
        self.editor.grid(row=0, column=1, sticky="nsew")
        self.editor_vscroll = ui.AutoScrollbar(self.editor_area, orient="vertical", command=self._on_editor_scroll, style="View.Vertical.TScrollbar")
        self.editor_vscroll.grid(row=0, column=2, sticky="ns")
        self.editor_hscroll = ui.AutoScrollbar(self.editor_area, orient="horizontal", command=self.editor.xview, style="View.Horizontal.TScrollbar")
        self.editor_hscroll.grid(row=1, column=1, sticky="we")
        self.editor.configure(yscrollcommand=self._on_editor_yscroll, xscrollcommand=self.editor_hscroll.set)
        self.editor_hint = tk.Label(
            self.editor, anchor="w",
            text="Type SQL here  ·  Ctrl+Enter runs the statement at the caret  ·  F5 runs everything",
        )
        self.editor_hint.bind("<Button-1>", lambda e: (self.editor.focus_set(), "break")[1])
        self.find_bar = ui.FindBar(self.editor_card, self)  # packs itself above editor_area on show()
        self.right.add(self.editor_card, weight=3)

        self.results_card = ttk.Frame(self.right, style="Card.TFrame", padding=1)
        footer = ttk.Frame(self.results_card, style="Sidebar.TFrame", padding=(px(10), px(2), px(4), px(2)))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(self.results_card).pack(side="bottom", fill="x")
        self.footer_label = ttk.Label(footer, text="", style="SidebarMuted.TLabel")
        self.footer_label.pack(side="left")
        self.export_btn = ttk.Button(footer, text="Export CSV", image=th.icon("export", "fg_muted"), compound="left",
                                     style="Sidebar.Toolbutton", command=self.export_csv, state="disabled")
        self.export_btn.pack(side="right")
        ui.Tooltip(self.export_btn, "Save this result set as a CSV file")
        # A batch can contain several statements, each producing its own
        # result set — one tab per result set, so none of them get clobbered
        # by the next. "Messages" is always the last tab.
        self.results_notebook = ttk.Notebook(self.results_card, style="Results.TNotebook")
        self.results_notebook.pack(side="top", fill="both", expand=True)
        self.results_notebook.bind("<<NotebookTabChanged>>", lambda e: self._sync_results_footer())
        self.messages_frame = ttk.Frame(self.results_notebook, style="View.TFrame")
        self.status_log = tk.Text(self.messages_frame, state="disabled")
        self.status_log.pack(side="left", fill="both", expand=True)
        log_scroll = ui.AutoScrollbar(self.messages_frame, orient="vertical", command=self.status_log.yview, style="View.Vertical.TScrollbar")
        log_scroll.pack(side="right", fill="y")
        self.status_log.configure(yscrollcommand=log_scroll.set)
        self._result_sheets = []
        self._result_meta = []
        self.results_notebook.add(self.messages_frame, text="Messages")
        self._add_placeholder_result_tab()
        self.right.add(self.results_card, weight=2)

    def _apply_initial_sash(self, event=None):
        if self._sash_applied:
            return
        self._sash_applied = True
        if self._sidebar_shown():
            try:
                self.main.sashpos(0, int(self.app.sidebar_width))
            except tk.TclError:
                pass

    def _sidebar_shown(self):
        # PanedWindow.panes() yields widget *names*, so compare by path.
        return str(self.sidebar) in [str(p) for p in self.main.panes()]

    def sidebar_width(self):
        try:
            if self._sidebar_shown():
                return int(self.main.sashpos(0))
        except tk.TclError:
            pass
        return None

    def apply_word_wrap(self, enabled):
        self.editor.configure(wrap="word" if enabled else "none")

    def apply_line_numbers_visibility(self, show):
        if show:
            self.line_numbers.grid()
        else:
            self.line_numbers.grid_remove()

    def apply_sidebar_visibility(self, show):
        present = self._sidebar_shown()
        if show and not present:
            self.main.insert(0, self.sidebar, weight=0)
            self._sash_applied = False
            self.root.after_idle(self._apply_initial_sash)
        elif not show and present:
            width = self.sidebar_width()
            if width:
                self.app.sidebar_width = width
            self.main.forget(self.sidebar)

    def apply_theme(self, pal):
        th = self.theme
        th.style_editor(self.editor)
        th.style_gutter(self.line_numbers)
        th.style_log(self.status_log)
        self.editor_hint.configure(background=pal.view, foreground=pal.fg_faint, font=th.fonts.ui.name)
        self.schema_tree.tag_configure("database", image=th.icon("database", "accent_text", pad_right=6))
        self.schema_tree.tag_configure("table", image=th.icon("table", "fg_muted", pad_right=6))
        self.schema_tree.tag_configure("column", image=th.icon("column", "fg_faint", pad_right=6))
        self.schema_tree.tag_configure("placeholder", foreground=pal.fg_faint)
        self.schema_tree.tag_configure("active", font=th.fonts.ui_bold.name)
        self.refresh_btn.configure(image=th.icon("refresh", "fg_muted"))
        self.export_btn.configure(image=th.icon_states("export", role="fg_muted"))
        opts = th.sheet_options()
        for sheet in self._result_sheets:
            try:
                sheet.set_options(redraw=False, **opts)
                sheet.config(bg=pal.view)
                nulls = getattr(sheet, "_np_null_cells", None)
                if nulls:
                    sheet.highlight_cells(cells=nulls, fg=pal.grid_null, redraw=False)
                sheet.redraw()
            except Exception:
                pass
        self.autocomplete.restyle()
        self._update_current_line()

    # ---------- status & messages ----------

    def _log_message(self, text, kind="info"):
        stamp = time.strftime("%H:%M:%S")
        self.status_log.configure(state="normal")
        self.status_log.insert("end", f"{stamp}  ", ("time",))
        tag = {"error": "error", "ok": "ok", "muted": "muted", "warning": "error"}.get(kind)
        self.status_log.insert("end", text + "\n", (tag,) if tag else ())
        self.status_log.see("end")
        self.status_log.configure(state="disabled")

    def set_status(self, text, kind="info", log=True):
        self.last_status = (text, kind)
        if log:
            self._log_message(text, kind)
        self.app._status_changed(self)

    def show_messages_tab(self):
        try:
            self.results_notebook.select(self.messages_frame)
        except tk.TclError:
            pass

    # ---------- editor scroll / line numbers ----------

    def _on_editor_scroll(self, *args):
        self.editor.yview(*args)
        self.line_numbers.yview(*args)

    def _on_editor_yscroll(self, first, last):
        self.editor_vscroll.set(first, last)
        self.line_numbers.yview_moveto(first)

    def _update_line_numbers(self):
        total_lines = int(self.editor.index("end-1c").split(".")[0])
        width = max(3, len(str(total_lines))) + 1
        content = "\n".join(str(i) for i in range(1, total_lines + 1))
        self.line_numbers.configure(state="normal", width=width)
        self.line_numbers.delete("1.0", "end")
        self.line_numbers.insert("1.0", content, ("right",))
        self.line_numbers.configure(state="disabled")
        self.line_numbers.yview_moveto(self.editor.yview()[0])
        self._update_current_line()

    def _update_current_line(self):
        try:
            self.editor.tag_remove("curline", "1.0", "end")
            self.editor.tag_add("curline", "insert linestart", "insert lineend+1c")
            line = int(self.editor.index("insert").split(".")[0])
            self.line_numbers.tag_remove("current", "1.0", "end")
            self.line_numbers.tag_add("current", f"{line}.0", f"{line}.end")
        except tk.TclError:
            pass

    def _update_editor_hint(self):
        px = self.theme.px
        if self.editor.get("1.0", "end-1c"):
            self.editor_hint.place_forget()
        else:
            self.editor_hint.place(x=px(10), y=px(8))

    def _on_caret_moved(self):
        self._update_current_line()
        self._update_bracket_match()
        self.app._update_cursor_position(self)

    # ---------- editor events ----------

    def _bind_editor_events(self):
        e = self.editor
        e.bind("<KeyRelease>", self._on_editor_key_release)
        e.bind("<Up>", lambda ev: "break" if self.autocomplete.move(-1) else None)
        e.bind("<Down>", lambda ev: "break" if self.autocomplete.move(1) else None)
        e.bind("<Return>", self._on_return)
        e.bind("<KP_Enter>", self._on_return)
        e.bind("<Tab>", self._on_tab)
        e.bind("<Shift-Tab>", self._on_shift_tab)
        for seq in ("<ISO_Left_Tab>", "<Shift-ISO_Left_Tab>"):
            try:
                e.bind(seq, self._on_shift_tab)
            except tk.TclError:
                pass
        e.bind("<Escape>", self._on_escape)
        e.bind("<ButtonPress-1>", lambda ev: self.autocomplete.hide())
        e.bind("<ButtonRelease-1>", lambda ev: self._on_caret_moved(), add="+")
        e.bind("<Button-3>", self._on_editor_right_click)
        e.bind("<<Modified>>", self._on_modified_flag)
        e.bind("<<Paste>>", lambda ev: self.root.after_idle(self._after_bulk_edit), add="+")
        e.bind("<<Cut>>", lambda ev: self.root.after_idle(self._after_bulk_edit), add="+")

        # Tk's Text widget has built-in Emacs-style bindings (Control-o inserts a
        # newline, Control-t transposes, Control-k kills to end of line, Control-h
        # backspaces, Control-f/b move, Control-a goes to line start, Control-Tab
        # moves focus...) that would otherwise fire — and silently mutate the
        # buffer — before our own shortcuts get a chance to run. Bind ours on the
        # widget itself and swallow the event.
        def shortcut(fn):
            def handler(event=None):
                fn()
                return "break"
            return handler

        e.bind("<Control-o>", shortcut(self.app.open_file))
        e.bind("<Control-t>", shortcut(self.app.new_tab))
        e.bind("<Control-n>", shortcut(self.app.new_tab))
        e.bind("<Control-k>", shortcut(self.open_connect_dialog))
        e.bind("<Control-f>", shortcut(self.open_find_bar))
        e.bind("<Control-h>", shortcut(self.open_replace_bar))
        e.bind("<Control-a>", shortcut(self.select_all))
        e.bind("<Control-slash>", shortcut(self.toggle_comment))
        e.bind("<Control-b>", shortcut(self.app.toggle_sidebar))
        e.bind("<Control-g>", shortcut(self.go_to_line))
        e.bind("<Control-p>", shortcut(self.app.open_palette))
        e.bind("<Control-e>", shortcut(self.run_statement))
        e.bind("<Control-Return>", shortcut(self.run_statement))
        e.bind("<Control-KP_Enter>", shortcut(self.run_statement))
        e.bind("<Control-Shift-Return>", shortcut(self.run_all))
        e.bind("<Control-y>", shortcut(self.redo))
        e.bind("<Control-Shift-Z>", shortcut(self.redo))
        e.bind("<Control-Tab>", shortcut(lambda: self.app.next_tab(1)))
        for seq in ("<Control-Shift-Tab>", "<Control-ISO_Left_Tab>", "<Control-Shift-ISO_Left_Tab>"):
            try:
                e.bind(seq, shortcut(lambda: self.app.next_tab(-1)))
            except tk.TclError:
                pass
        e.bind("<Alt-Up>", lambda ev: self.move_line(-1))
        e.bind("<Alt-Down>", lambda ev: self.move_line(1))
        e.bind("<Control-MouseWheel>", lambda ev: (self.app.zoom(1 if ev.delta > 0 else -1), "break")[1])
        e.bind("<Control-Button-4>", lambda ev: (self.app.zoom(1), "break")[1])
        e.bind("<Control-Button-5>", lambda ev: (self.app.zoom(-1), "break")[1])

    def _after_bulk_edit(self):
        self.highlight_syntax()
        self._update_line_numbers()
        self._update_editor_hint()
        self._on_caret_moved()

    def _on_modified_flag(self, event):
        modified = self.editor.edit_modified()
        if modified != self.dirty:
            self.dirty = modified
            self._refresh_tab_label()

    def _mark_clean(self):
        self.editor.edit_modified(False)
        self.dirty = False
        self._refresh_tab_label()

    def _on_editor_key_release(self, event):
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next",
                            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R"):
            self._on_caret_moved()
            return
        if event.keysym in ("Return", "KP_Enter", "Tab", "Escape"):
            self._on_caret_moved()
            return
        self.highlight_syntax()
        self._update_line_numbers()
        self._update_editor_hint()
        if event.state & 0x4 and event.keysym.lower() not in ("v",):  # Control held: no completions
            self.autocomplete.hide()
        else:
            self.autocomplete.update_suggestions()
        self._on_caret_moved()

    def _on_return(self, event):
        if self.autocomplete.visible():
            return self.autocomplete.accept()
        line_start = self.editor.index("insert linestart")
        line_end = self.editor.index("insert lineend")
        current_line = self.editor.get(line_start, line_end)
        indent = re.match(r"[ \t]*", current_line).group(0)
        self.editor.insert(tk.INSERT, "\n" + indent)
        self.editor.see(tk.INSERT)
        self.highlight_syntax()
        self.root.after_idle(self._update_line_numbers)
        self.root.after_idle(self._on_caret_moved)
        return "break"

    def _on_tab(self, event):
        if self.autocomplete.visible():
            return self.autocomplete.accept()
        try:
            first = self.editor.index("sel.first")
            last = self.editor.index("sel.last")
            if first.split(".")[0] != last.split(".")[0]:
                return self._indent_selection(outdent=False)
        except tk.TclError:
            pass
        self.editor.insert(tk.INSERT, "    ")
        return "break"

    def _on_shift_tab(self, event):
        return self._indent_selection(outdent=True)

    def _indent_selection(self, outdent):
        e = self.editor
        try:
            first = int(e.index("sel.first").split(".")[0])
            last_idx = e.index("sel.last")
            last = int(last_idx.split(".")[0])
            if last > first and last_idx.endswith(".0"):
                last -= 1
            had_sel = True
        except tk.TclError:
            first = last = int(e.index("insert").split(".")[0])
            had_sel = False
        for ln in range(first, last + 1):
            if outdent:
                line = e.get(f"{ln}.0", f"{ln}.end")
                spaces = len(line) - len(line.lstrip(" "))
                remove = min(4, spaces) if spaces else (1 if line.startswith("\t") else 0)
                if remove:
                    e.delete(f"{ln}.0", f"{ln}.{remove}")
            else:
                e.insert(f"{ln}.0", "    ")
        if had_sel:
            e.tag_remove("sel", "1.0", "end")
            e.tag_add("sel", f"{first}.0", f"{last}.end")
        self.highlight_syntax()
        self._on_caret_moved()
        return "break"

    def _on_escape(self, event):
        if self.autocomplete.visible():
            self.autocomplete.hide()
            return "break"
        if self.find_bar.visible():
            self.find_bar.hide()
            return "break"
        return None

    def select_all(self):
        self.editor.tag_remove("sel", "1.0", "end")
        self.editor.tag_add("sel", "1.0", "end-1c")
        self.editor.mark_set(tk.INSERT, "end-1c")
        return "break"

    def redo(self):
        try:
            self.editor.edit_redo()
        except tk.TclError:
            pass
        self._after_bulk_edit()

    def undo(self):
        try:
            self.editor.edit_undo()
        except tk.TclError:
            pass
        self._after_bulk_edit()

    def go_to_line(self):
        total = int(self.editor.index("end-1c").split(".")[0])
        current = int(self.editor.index("insert").split(".")[0])

        def validate(value):
            value = value.strip()
            if not value.isdigit() or not (1 <= int(value) <= total):
                return f"Enter a line number between 1 and {total}."
            return None

        value = ui.ask_string(self.root, "Go to line", f"Line number (1–{total})", initial=str(current), ok="Go", validate=validate)
        if value:
            self.editor.mark_set(tk.INSERT, f"{int(value.strip())}.0")
            self.editor.see(tk.INSERT)
            self.editor.focus_set()
            self._on_caret_moved()

    def move_line(self, delta):
        e = self.editor
        try:
            first = int(e.index("sel.first").split(".")[0])
            last_idx = e.index("sel.last")
            last = int(last_idx.split(".")[0])
            if last > first and last_idx.endswith(".0"):
                last -= 1
            had_sel = True
        except tk.TclError:
            first = last = int(e.index("insert").split(".")[0])
            had_sel = False
        total = int(e.index("end-1c").split(".")[0])
        if (delta < 0 and first == 1) or (delta > 0 and last >= total):
            return "break"
        caret_line, caret_col = (int(v) for v in e.index("insert").split("."))
        block = e.get(f"{first}.0", f"{last}.end")
        if delta < 0:
            other = e.get(f"{first - 1}.0", f"{first - 1}.end")
            e.delete(f"{first - 1}.0", f"{last}.end")
            e.insert(f"{first - 1}.0", block + "\n" + other)
            new_first = first - 1
        else:
            other = e.get(f"{last + 1}.0", f"{last + 1}.end")
            e.delete(f"{first}.0", f"{last + 1}.end")
            e.insert(f"{first}.0", other + "\n" + block)
            new_first = first + 1
        new_last = new_first + (last - first)
        e.tag_remove("sel", "1.0", "end")
        if had_sel:
            e.tag_add("sel", f"{new_first}.0", f"{new_last}.end")
        e.mark_set(tk.INSERT, f"{caret_line + delta}.{caret_col}")
        e.see(tk.INSERT)
        self.highlight_syntax()
        self._update_line_numbers()
        self._on_caret_moved()
        return "break"

    def _on_editor_right_click(self, event):
        self.editor.focus_set()
        menu = tk.Menu(self.root, tearoff=0)
        has_sel = bool(self.editor.tag_ranges("sel"))
        menu.add_command(label="Undo", command=self.undo, accelerator="Ctrl+Z")
        menu.add_command(label="Redo", command=self.redo, accelerator="Ctrl+Y")
        menu.add_separator()
        menu.add_command(label="Cut", command=lambda: self.editor.event_generate("<<Cut>>"), accelerator="Ctrl+X",
                         state="normal" if has_sel else "disabled")
        menu.add_command(label="Copy", command=lambda: self.editor.event_generate("<<Copy>>"), accelerator="Ctrl+C",
                         state="normal" if has_sel else "disabled")
        menu.add_command(label="Paste", command=lambda: self.editor.event_generate("<<Paste>>"), accelerator="Ctrl+V")
        menu.add_command(label="Select all", command=self.select_all, accelerator="Ctrl+A")
        menu.add_separator()
        menu.add_command(label="Run selection" if has_sel else "Run statement", command=self.run_statement, accelerator="Ctrl+Enter")
        menu.add_command(label="Explain plan", command=self.explain_query, accelerator="Ctrl+L")
        menu.add_separator()
        menu.add_command(label="Toggle comment", command=self.toggle_comment, accelerator="Ctrl+/")
        menu.add_command(label="Format SQL", command=self.format_sql, accelerator="Ctrl+Shift+F")
        self.theme.style_menu(menu)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def highlight_syntax(self):
        highlight_sql_text(self.editor)

    def _update_bracket_match(self):
        self.editor.tag_remove("bracket", "1.0", "end")
        text = self.editor.get("1.0", "end-1c")
        cursor_offset = len(self.editor.get("1.0", tk.INSERT))
        for pos in (cursor_offset, cursor_offset - 1):
            if pos < 0 or pos >= len(text):
                continue
            ch = text[pos]
            if ch in BRACKET_PAIRS:
                match = _find_bracket_match(text, pos, ch, BRACKET_PAIRS[ch], forward=True)
            elif ch in BRACKET_PAIRS_REVERSE:
                match = _find_bracket_match(text, pos, BRACKET_PAIRS_REVERSE[ch], ch, forward=False)
            else:
                continue
            if match is not None:
                self.editor.tag_add("bracket", f"1.0+{pos}c", f"1.0+{pos + 1}c")
                self.editor.tag_add("bracket", f"1.0+{match}c", f"1.0+{match + 1}c")
                break

    def toggle_comment(self, event=None):
        try:
            sel_start = self.editor.index("sel.first linestart")
            sel_end = self.editor.index("sel.last lineend")
            has_selection = True
        except tk.TclError:
            sel_start = self.editor.index("insert linestart")
            sel_end = self.editor.index("insert lineend")
            has_selection = False
        lines = self.editor.get(sel_start, sel_end).split("\n")
        non_blank = [ln for ln in lines if ln.strip()]
        all_commented = bool(non_blank) and all(ln.lstrip().startswith("--") for ln in non_blank)
        if all_commented:
            new_lines = [re.sub(r"^(\s*)--\s?", r"\1", ln) for ln in lines]
        else:
            new_lines = [("-- " + ln) if ln.strip() else ln for ln in lines]
        new_text = "\n".join(new_lines)
        self.editor.delete(sel_start, sel_end)
        self.editor.insert(sel_start, new_text)
        if has_selection:
            self.editor.tag_remove("sel", "1.0", "end")
            self.editor.tag_add("sel", sel_start, f"{sel_start}+{len(new_text)}c")
        self.highlight_syntax()
        self._update_line_numbers()
        self._on_caret_moved()
        return "break"

    # ---------- schema tree ----------

    def _schedule_filter(self):
        if self._filter_after is not None:
            try:
                self.root.after_cancel(self._filter_after)
            except tk.TclError:
                pass
        self._filter_after = self.root.after(150, self._apply_filter)

    def _apply_filter(self):
        self._filter_after = None
        self.populate_schema_tree()

    def _sync_sidebar_state(self):
        connected = self.backend is not None and self.backend.is_connected
        if connected:
            self.sidebar_empty.place_forget()
        else:
            self.sidebar_empty.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.sidebar_empty.lift()

    def populate_schema_tree(self):
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        self._tooltip_item = None
        self.schema_tree.delete(*self.schema_tree.get_children())
        self._sync_sidebar_state()
        if not self.databases:
            return
        filt = self.filter_var.get().strip().lower()
        for db_name in sorted(self.databases, key=str.lower):
            is_active = db_name == self.active_database
            db_node = self.schema_tree.insert(
                "", "end", text=db_name, tags=("database", "active") if is_active else ("database",),
                open=is_active,
            )
            if is_active:
                self.schema_tree.item(db_node, text=f"{db_name}  (active)")
            cached = self.db_schema_cache.get(db_name)
            if cached is not None:
                self._populate_tables(db_node, cached, filt)
            else:
                self.schema_tree.insert(db_node, "end", text="Expand to load tables", tags=("placeholder",))

    def _populate_tables(self, db_node, schema, filt):
        for table_key in sorted(schema.keys()):
            cols = schema[table_key]  # [(col_name, type_str), ...]
            if filt and filt not in table_key.lower() and not any(filt in name.lower() for name, _ in cols):
                continue
            table_node = self.schema_tree.insert(
                db_node, "end", text=table_key, values=(table_key,), open=bool(filt), tags=("table",),
            )
            for col_name, col_type in cols:
                if filt and filt not in table_key.lower() and filt not in col_name.lower():
                    continue
                self.schema_tree.insert(
                    table_node, "end", text=f"{col_name}  {col_type}", values=(col_name,), tags=("column",),
                )

    def _on_tree_open(self, event):
        item = self.schema_tree.focus()
        if not item or "database" not in self.schema_tree.item(item, "tags"):
            return
        db_name = self._tree_item_label(item)
        if db_name in self.db_schema_cache:
            return
        self._load_database_schema(item, db_name)

    def _on_tree_double_click(self, event):
        item = self.schema_tree.identify_row(event.y)
        if not item:
            return
        tags = self.schema_tree.item(item, "tags")
        if "table" in tags:
            table_key = self.schema_tree.item(item, "values")[0]
            self._select_top_rows(table_key)
            return "break"
        if "database" in tags:
            db_name = self._tree_item_label(item)
            if db_name != self.active_database:
                self._set_active_database(db_name)
        return None

    def _load_database_schema(self, db_node, db_name):
        self.set_status(f"Loading tables for {db_name}…", "busy", log=False)

        def work():
            return self.backend.list_schema(db_name)

        def done(result, error):
            if error is not None:
                self.set_status(f"Couldn't load {db_name}: {error}", "error")
                return
            self.db_schema_cache[db_name] = result
            if not self.schema_tree.exists(db_node):
                return
            self.schema_tree.delete(*self.schema_tree.get_children(db_node))
            filt = self.filter_var.get().strip().lower()
            self._populate_tables(db_node, result, filt)
            self.set_status(f"Loaded {_plural(len(result), 'table')} from {db_name}.", "muted")

        self._run_async(work, done)

    def _set_active_database(self, db_name):
        if self.backend is None or not self.backend.is_connected:
            return
        if db_name == self.active_database:
            return
        self.set_status(f"Switching to {db_name}…", "busy", log=False)

        def work():
            self.backend.use_database(db_name)
            return self.db_schema_cache.get(db_name) or self.backend.list_schema(db_name)

        def done(result, error):
            if error is not None:
                self.set_status(f"Couldn't switch to {db_name}: {error}", "error")
                ui.show_error(self.root, "Couldn't switch database", str(error))
                return
            self.active_database = db_name
            self.db_schema_cache[db_name] = result
            self.schema = result
            self._auto_title(db_name)
            self.app._sync_chrome_if_active(self)
            self.set_status(f"Using {db_name} · {_plural(len(result), 'table')}.", "ok")
            self.populate_schema_tree()
            existing = load_config()
            existing["server"] = self.connected_server
            existing["database"] = db_name
            existing["recent_databases"] = _with_recent(existing.get("recent_databases", []), db_name)
            save_config(existing)

        self._run_async(work, done)

    def connection_label(self):
        """(text, state) for the status bar / toolbar badge."""
        if self.backend is None or not self.backend.is_connected:
            return "Not connected", "disconnected"
        server = self.connected_server or ""
        if self.backend.label == "SQLite":
            server = os.path.basename(server)
        if self.active_database and self.active_database != server:
            text = f"{server} / {self.active_database}"
        else:
            text = server
        return f"{text}  ·  {self.backend.label}", "connected"

    def short_connection_label(self):
        if self.backend is None or not self.backend.is_connected:
            return None
        if self.active_database:
            return self.active_database
        server = self.connected_server or ""
        return os.path.basename(server) if self.backend.label == "SQLite" else server

    def _on_tree_right_click(self, event):
        item = self.schema_tree.identify_row(event.y)
        if not item:
            return
        tags = self.schema_tree.item(item, "tags")
        self.schema_tree.selection_set(item)
        self.schema_tree.focus(item)
        menu = tk.Menu(self.root, tearoff=0)
        if "database" in tags:
            db_name = self._tree_item_label(item)
            menu.add_command(label=f"Use “{db_name}”", command=lambda: self._set_active_database(db_name),
                             state="disabled" if db_name == self.active_database else "normal")
            menu.add_command(label="Refresh schema", command=self.refresh_schema)
        elif "table" in tags:
            table_key = self.schema_tree.item(item, "values")[0]
            menu.add_command(label="Select top 1000 rows", command=lambda: self._select_top_rows(table_key))
            menu.add_command(label="Script as CREATE TABLE", command=lambda: self._script_table_ddl(table_key))
            menu.add_command(label="Copy column list", command=lambda: self._copy_column_list(table_key))
            menu.add_command(label="Copy table name", command=lambda: self._copy_to_clipboard(table_key, "table name"))
            menu.add_separator()
            menu.add_command(label="Export table to CSV…", command=lambda: self._export_table_csv(table_key))
        elif "column" in tags:
            col_name = self.schema_tree.item(item, "values")[0]
            menu.add_command(label="Insert column name", command=lambda: self._insert_at_caret(col_name))
            menu.add_command(label="Copy column name", command=lambda: self._copy_to_clipboard(col_name, "column name"))
        else:
            return
        self.theme.style_menu(menu)
        menu.tk_popup(event.x_root, event.y_root)

    # ---------- table context menu actions ----------

    def _cols_for_table(self, table_key):
        if self.schema and table_key in self.schema:
            return self.schema[table_key]
        for db_schema in self.db_schema_cache.values():
            if table_key in db_schema:
                return db_schema[table_key]
        return None

    def _copy_to_clipboard(self, text, what="text"):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied {what} to clipboard.", "muted", log=False)

    def _insert_at_caret(self, text):
        self.editor.insert(tk.INSERT, text)
        self.editor.focus_set()
        self._after_bulk_edit()

    def _insert_text(self, sql):
        """Append `sql` on its own line at the end of the buffer and select
        it, so Run (which runs the selection if there is one) acts on just
        this statement regardless of whatever else is in the editor."""
        editor = self.editor
        current = editor.get("1.0", "end-1c")
        if current and not current.endswith("\n"):
            editor.insert("end", "\n")
        start_index = editor.index("end-1c")
        editor.insert("end", sql)
        end_index = editor.index("end-1c")
        editor.tag_remove("sel", "1.0", "end")
        editor.tag_add("sel", start_index, end_index)
        editor.mark_set(tk.INSERT, end_index)
        editor.see(end_index)
        self._after_bulk_edit()

    def _insert_and_run(self, sql):
        self._insert_text(sql)
        self.run_query()

    def _select_top_rows(self, table_key):
        if self.backend is None or not self.backend.is_connected:
            return
        tab = self.app.new_tab()
        tab.backend = self.backend
        tab.connected_server = self.connected_server
        tab._last_auth_mode = self._last_auth_mode
        tab._last_port = self._last_port
        tab._last_encrypt = self._last_encrypt
        tab._last_trust_cert = self._last_trust_cert
        tab.databases = self.databases
        tab.db_schema_cache = self.db_schema_cache
        tab.active_database = self.active_database
        tab.schema = self.schema
        tab._auto_title(self.active_database or self.connected_server)
        tab.populate_schema_tree()
        self.app._sync_chrome_if_active(tab)
        tab._insert_and_run(self.backend.select_top_sql(table_key))

    def _script_table_ddl(self, table_key):
        cols = self._cols_for_table(table_key)
        if cols is None:
            self.set_status("Expand the table first so its columns are loaded.", "warning", log=False)
            return
        lines = ",\n".join(f"    {_quote_ident(name)} {type_str}" for name, type_str in cols)
        ddl = f"CREATE TABLE {_quote_table_key(table_key)} (\n{lines}\n);"
        tab = self.app.new_tab()
        tab.editor.insert("1.0", ddl)
        tab.set_title(f"{table_key} DDL")
        tab._mark_clean()
        tab._after_bulk_edit()

    def _copy_column_list(self, table_key):
        cols = self._cols_for_table(table_key)
        if cols is None:
            self.set_status("Expand the table first so its columns are loaded.", "warning", log=False)
            return
        text = ", ".join(name for name, _ in cols)
        self._copy_to_clipboard(text, _plural(len(cols), "column name"))

    def _copy_cells_with_headers(self, sheet):
        min_r, min_c, max_r, max_c = sheet.get_selected_min_max()
        if min_r is None:
            return
        headers = sheet.headers()
        data = sheet.get_sheet_data()
        lines = ["\t".join(str(headers[c]) for c in range(min_c, max_c))]
        for r in range(min_r, max_r):
            lines.append("\t".join(str(data[r][c]) for c in range(min_c, max_c)))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.set_status(f"Copied {_plural(max_r - min_r, 'row')} with headers to clipboard.", "muted", log=False)

    def _export_table_csv(self, table_key):
        if self.backend is None or not self.backend.is_connected:
            return
        self.set_status(f"Exporting {table_key}…", "busy", log=False)

        def work():
            sql = self.backend.select_top_sql(table_key, limit=100000)
            return self.backend.execute(sql)

        def done(result, error):
            if error is not None:
                self.set_status(f"Export failed: {error}", "error")
                return
            columns, rows = result
            path = ui.pick_save_file(self.root, "Export table to CSV", CSV_FILETYPES,
                                     initialfile=f"{table_key.rsplit('.', 1)[-1]}.csv", defaultextension=".csv")
            if not path:
                self.set_status("Export cancelled.", "muted", log=False)
                return
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(columns)
                    writer.writerows(rows)
            except Exception as exc:
                self.set_status(f"Export failed: {exc}", "error")
                ui.show_error(self.root, "Export failed", str(exc))
                return
            self.set_status(f"Exported {_plural(len(rows), 'row')} to {path}", "ok")

        self._run_async(work, done)

    # ---------- tree tooltips ----------

    def _on_tree_hover(self, event):
        if self._drag_data and self._drag_data.get("active"):
            self._hide_tooltip()
            return
        item = self.schema_tree.identify_row(event.y)
        if item == self._tooltip_item:
            return
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        self._tooltip_item = item
        if not item:
            return
        self._tooltip_after_id = self.root.after(
            600, lambda: self._show_tooltip(item, event.x_root, event.y_root)
        )

    def _on_tree_leave(self, event):
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        self._tooltip_item = None

    def _cancel_tooltip_timer(self):
        if self._tooltip_after_id is not None:
            try:
                self.root.after_cancel(self._tooltip_after_id)
            except Exception:
                pass
            self._tooltip_after_id = None

    def _show_tooltip(self, item, x_root, y_root):
        self._tooltip_after_id = None
        if not self.schema_tree.exists(item) or item != self._tooltip_item:
            return
        text = self.schema_tree.item(item, "text")
        if not text:
            return
        tags = self.schema_tree.item(item, "tags")
        if "table" in tags:
            cols = self._cols_for_table(self.schema_tree.item(item, "values")[0])
            if cols:
                text = f"{text}\n{_plural(len(cols), 'column')} · double-click to select top 1000 rows"
        self._hide_tooltip()
        pal = self.theme.pal
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tip.configure(background=pal.tooltip_bg)
        tip.geometry(f"+{x_root + 14}+{y_root + 10}")
        tk.Label(
            tip, text=text, background=pal.tooltip_bg, foreground=pal.tooltip_fg, justify="left",
            font=self.theme.fonts.small.name, padx=self.theme.px(10), pady=self.theme.px(6),
        ).pack()
        self._tooltip_window = tip

    def _hide_tooltip(self):
        if self._tooltip_window is not None:
            try:
                self._tooltip_window.destroy()
            except Exception:
                pass
            self._tooltip_window = None

    def _tree_item_label(self, item):
        return self.schema_tree.item(item, "text").split("  (active)")[0]

    def _tree_item_search_key(self, item):
        # Table nodes are labeled "schema.table" — search by the bare table
        # name so typing doesn't require the schema prefix first.
        label = self._tree_item_label(item)
        return label.rsplit(".", 1)[-1] if "." in label else label

    def _visible_tree_items(self):
        items = []

        def walk(parent):
            for child in self.schema_tree.get_children(parent):
                items.append(child)
                if self.schema_tree.item(child, "open"):
                    walk(child)

        walk("")
        return items

    def _on_tree_keypress(self, event):
        if event.keysym in (
            "Up", "Down", "Left", "Right", "Return", "Tab", "Escape", "Prior", "Next", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        ):
            if event.keysym == "Escape":
                self.editor.focus_set()
            return None
        if event.state & 0x4:  # Control held: let the global shortcuts run
            return None
        ch = event.char
        if not ch or not ch.isprintable():
            return None

        now = time.monotonic()
        if now - self._tree_search_last_time > 0.8:
            self._tree_search_buffer = ""
        self._tree_search_last_time = now
        self._tree_search_buffer += ch.lower()

        items = self._visible_tree_items()
        if not items:
            return "break"

        def find(prefix):
            return next(
                (it for it in items if self._tree_item_search_key(it).lower().startswith(prefix)),
                None,
            )

        match = find(self._tree_search_buffer)
        if match is None and len(self._tree_search_buffer) > 1:
            # No match for the accumulated prefix — restart the search with
            # just this keystroke, as if it were the first one typed.
            self._tree_search_buffer = ch.lower()
            match = find(self._tree_search_buffer)

        if match is not None:
            self.schema_tree.selection_set(match)
            self.schema_tree.focus(match)
            self.schema_tree.see(match)
        return "break"

    # ---------- drag-and-drop: schema tree -> editor ----------

    def _on_tree_press(self, event):
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        item = self.schema_tree.identify_row(event.y)
        values = self.schema_tree.item(item, "values") if item else None
        if not values:
            self._drag_data = None
            return
        self._drag_data = {
            "text": values[0],
            "start_x": event.x_root,
            "start_y": event.y_root,
            "active": False,
        }

    def _on_tree_motion(self, event):
        data = self._drag_data
        if not data:
            return
        if not data["active"]:
            dx = abs(event.x_root - data["start_x"])
            dy = abs(event.y_root - data["start_y"])
            if dx < self.DRAG_THRESHOLD_PX and dy < self.DRAG_THRESHOLD_PX:
                return
            data["active"] = True
            self.autocomplete.hide()
            self._start_drag_ghost(data["text"])
        self._move_drag_ghost(event.x_root, event.y_root)
        self._update_drag_target_feedback(event.x_root, event.y_root)

    def _on_tree_release(self, event):
        data = self._drag_data
        self._drag_data = None
        if data and data["active"]:
            self._drop_text_at(event.x_root, event.y_root, data["text"])
        self._end_drag_ghost()

    def _start_drag_ghost(self, text):
        pal = self.theme.pal
        self._drag_ghost = tk.Toplevel(self.root)
        self._drag_ghost.wm_overrideredirect(True)
        try:
            self._drag_ghost.attributes("-topmost", True)
            self._drag_ghost.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        self._drag_ghost.configure(background=pal.border)
        self._drag_ghost_label = tk.Label(
            self._drag_ghost, text=text, font=self.theme.fonts.mono_small.name,
            bg=pal.popover, fg=pal.fg, padx=self.theme.px(8), pady=self.theme.px(4),
        )
        self._drag_ghost_label.pack(padx=1, pady=1)

    def _move_drag_ghost(self, x_root, y_root):
        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{x_root + 14}+{y_root + 12}")

    def _update_drag_target_feedback(self, x_root, y_root):
        if self._drag_ghost is None:
            return
        pal = self.theme.pal
        over_editor = self.root.winfo_containing(x_root, y_root) is self.editor
        self._drag_ghost_label.configure(bg=pal.accent_soft if over_editor else pal.popover)
        self._drag_ghost.configure(background=pal.accent if over_editor else pal.border)

    def _end_drag_ghost(self):
        if self._drag_ghost is not None:
            self._drag_ghost.destroy()
            self._drag_ghost = None
            self._drag_ghost_label = None

    def _drop_text_at(self, x_root, y_root, text):
        if self.root.winfo_containing(x_root, y_root) is not self.editor:
            return
        x = x_root - self.editor.winfo_rootx()
        y = y_root - self.editor.winfo_rooty()
        index = self.editor.index(f"@{x},{y}")
        self.editor.mark_set(tk.INSERT, index)
        self.editor.insert(index, text)
        self.editor.focus_set()
        self._after_bulk_edit()

    # ---------- file operations ----------

    def load_file(self, path):
        content = read_text_file(path)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", content)
        self.editor.edit_reset()
        self.current_file = path
        self.set_title(os.path.basename(path))
        self._mark_clean()
        self._after_bulk_edit()
        self.editor.mark_set(tk.INSERT, "1.0")
        self.set_status(f"Opened {path}", "muted")

    def save_file(self):
        if not self.current_file:
            return self.save_file_as()
        try:
            with open(self.current_file, "w", encoding="utf-8") as f:
                f.write(self.editor.get("1.0", "end-1c"))
        except OSError as exc:
            ui.show_error(self.root, "Couldn't save file", str(exc))
            return
        self._mark_clean()
        self.set_status(f"Saved {self.current_file}", "ok", log=False)

    def save_file_as(self):
        initial = self.title if self.title.lower().endswith(".sql") else f"{self.title}.sql"
        path = ui.pick_save_file(
            self.root, "Save SQL file", SQL_FILETYPES, initialfile=initial, defaultextension=".sql",
            initialdir=os.path.dirname(self.current_file) if self.current_file else None,
        )
        if not path:
            return
        self.current_file = path
        self.set_title(os.path.basename(path))
        self.save_file()

    def set_title(self, title):
        self.title = title
        self._refresh_tab_label()

    def _refresh_tab_label(self):
        label = self.title + (" •" if self.dirty else "")
        try:
            self.app.notebook.tab(self.frame, text=label)
        except tk.TclError:
            pass
        self.app._update_title()

    def _auto_title(self, base_name):
        # Mirrors SSMS's "SQLQuery1, SQLQuery2, ..." but keyed off the actual
        # database name instead of a generic prefix. Only kicks in before the
        # user has explicitly saved the tab to a file of their own choosing.
        if self.current_file is not None:
            return
        if base_name and self.backend is not None and self.backend.label == "SQLite":
            base_name = os.path.splitext(os.path.basename(base_name))[0] or base_name
        taken = {t.title for t in self.app.tabs if t is not self}
        n = 1
        while f"{base_name}{n}" in taken:
            n += 1
        self.set_title(f"{base_name}{n}")

    # ---------- connection ----------

    def open_connect_dialog(self):
        result = ConnectDialog(self.root, load_config()).show()
        if result is None:
            self.editor.focus_set()
            return
        self._connect_with(result)

    def _connect_with(self, params):
        target = os.path.basename(params["server"]) if params["engine"] == "sqlite" else params["server"]
        self.set_status(f"Connecting to {target}…", "busy")
        backend_cls = BACKENDS[params["engine"]]
        backend = backend_cls()

        def work():
            backend.connect(
                server=params["server"],
                database=params["database"],
                auth_mode=params["auth_mode"],
                username=params["username"],
                password=params["password"],
                encrypt=params["encrypt"],
                trust_cert=params["trust_cert"],
                port=params["port"],
            )
            databases = backend.list_databases()
            active_schema = backend.list_schema(params["database"]) if params["database"] else None
            return backend, databases, active_schema, params

        def done(result, error):
            if error is not None:
                self.set_status(f"Connection failed: {error}", "error")
                ui.show_error(self.root, "Couldn't connect", str(error))
                return
            backend, databases, active_schema, params = result
            if self.backend is not None and self.backend is not backend:
                try:
                    self.backend.disconnect()
                except Exception:
                    pass
            self.backend = backend
            self.connected_server = params["server"]
            self._last_auth_mode = params["auth_mode"]
            self._last_port = params["port"]
            self._last_encrypt = params["encrypt"]
            self._last_trust_cert = params["trust_cert"]
            self.databases = databases
            self.db_schema_cache = {}
            self.active_database = params["database"]
            if active_schema is not None:
                self.db_schema_cache[self.active_database] = active_schema
            self.schema = active_schema
            self._auto_title(self.active_database or self.connected_server)
            self.populate_schema_tree()
            self.app._sync_chrome_if_active(self)
            if self.active_database:
                self.set_status(f"Connected to {target} · {_plural(len(active_schema), 'table')} loaded.", "ok")
            else:
                self.set_status(f"Connected to {target} · {_plural(len(databases), 'database')} — pick one to drill in.", "ok")
            to_save = dict(params)
            to_save.pop("password", None)
            existing = load_config()
            existing.update(to_save)
            existing["recent_servers"] = _with_recent(existing.get("recent_servers", []), params["server"])
            existing["recent_databases"] = _with_recent(existing.get("recent_databases", []), params["database"] or "")
            save_config(existing)
            self.editor.focus_set()

        self._run_async(work, done)

    def disconnect(self):
        if self.backend is not None:
            self.backend.disconnect()
        self.backend = None
        self.connected_server = None
        self.databases = []
        self.db_schema_cache = {}
        self.active_database = None
        self.schema = None
        self.populate_schema_tree()
        self.app._sync_chrome_if_active(self)
        self.set_status("Disconnected.", "muted")

    def refresh_schema(self):
        if self.backend is None or not self.backend.is_connected:
            self.set_status("Connect to a database first (Ctrl+K).", "warning", log=False)
            return
        self.set_status("Refreshing schema…", "busy", log=False)

        def work():
            databases = self.backend.list_databases()
            active_schema = self.backend.list_schema(self.active_database) if self.active_database else None
            return databases, active_schema

        def done(result, error):
            if error is not None:
                self.set_status(f"Schema refresh failed: {error}", "error")
                return
            databases, active_schema = result
            self.databases = databases
            self.db_schema_cache = {}
            if self.active_database:
                self.db_schema_cache[self.active_database] = active_schema
            self.schema = active_schema
            self.populate_schema_tree()
            self.set_status(f"Schema refreshed · {_plural(len(databases), 'database')}.", "ok")

        self._run_async(work, done)

    # ---------- find / replace ----------

    def _selection_seed(self):
        try:
            text = self.editor.get("sel.first", "sel.last")
        except tk.TclError:
            return None
        if text and "\n" not in text and len(text) <= 120:
            return text
        return None

    def open_find_bar(self):
        self.autocomplete.hide()
        self.find_bar.show(replace=False, seed=self._selection_seed())

    def open_replace_bar(self):
        self.autocomplete.hide()
        self.find_bar.show(replace=True, seed=self._selection_seed())

    def _find(self, query, start, backwards):
        if backwards:
            return self.editor.search(
                query, start, stopindex="1.0", backwards=True, nocase=not self.match_case_var.get(),
            )
        return self.editor.search(
            query, start, stopindex="end", nocase=not self.match_case_var.get(),
        )

    def _focus_in_find_bar(self):
        try:
            focus = self.root.focus_get()
        except (KeyError, tk.TclError):
            return False
        return focus is not None and self.find_bar.visible() and str(focus).startswith(str(self.find_bar))

    def _select_match(self, idx, query, place_cursor_at_start, focus=None):
        end = f"{idx}+{len(query)}c"
        self.editor.tag_remove("sel", "1.0", "end")
        self.editor.tag_add("sel", idx, end)
        self.editor.mark_set(tk.INSERT, idx if place_cursor_at_start else end)
        self.editor.see(idx)
        if focus is None:
            focus = not self._focus_in_find_bar()
        if focus:
            self.editor.focus_set()
        self._on_caret_moved()

    def find_next(self):
        query = self.find_var.get()
        if not query:
            return
        try:
            start = self.editor.index("sel.last")
        except tk.TclError:
            start = self.editor.index(tk.INSERT)
        idx = self._find(query, start, backwards=False) or self._find(query, "1.0", backwards=False)
        if not idx:
            self.set_status(f"“{query}” not found.", "warning", log=False)
            self.refresh_find_highlights()
            return
        self._select_match(idx, query, place_cursor_at_start=False)
        self.refresh_find_highlights()

    def find_previous(self):
        query = self.find_var.get()
        if not query:
            return
        try:
            start = self.editor.index("sel.first")
        except tk.TclError:
            start = self.editor.index(tk.INSERT)
        idx = self._find(query, start, backwards=True) or self._find(query, "end", backwards=True)
        if not idx:
            self.set_status(f"“{query}” not found.", "warning", log=False)
            self.refresh_find_highlights()
            return
        self._select_match(idx, query, place_cursor_at_start=True)
        self.refresh_find_highlights()

    def refresh_find_highlights(self, reposition=False):
        e = self.editor
        e.tag_remove("findmatch", "1.0", "end")
        query = self.find_var.get()
        if not query or not self.find_bar.visible():
            self.find_bar.set_count(None, 0)
            return
        nocase = not self.match_case_var.get()
        count = tk.IntVar(master=self.root)
        idx = "1.0"
        positions = []
        while len(positions) < 5000:
            idx = e.search(query, idx, stopindex="end", nocase=nocase, count=count)
            if not idx or count.get() == 0:
                break
            end = f"{idx}+{count.get()}c"
            e.tag_add("findmatch", idx, end)
            positions.append(e.index(idx))
            idx = end
        total = len(positions)
        current = None
        try:
            sel_first = e.index("sel.first")
            sel_text = e.get("sel.first", "sel.last")
            same = sel_text == query if not nocase else sel_text.lower() == query.lower()
            if same and sel_first in positions:
                current = positions.index(sel_first) + 1
        except tk.TclError:
            pass
        if reposition and total and current is None:
            try:
                start = e.index("sel.first")
            except tk.TclError:
                start = e.index(tk.INSERT)
            hit = self._find(query, start, backwards=False) or self._find(query, "1.0", backwards=False)
            if hit:
                self._select_match(hit, query, place_cursor_at_start=True, focus=False)
                hit = e.index(hit)
                current = positions.index(hit) + 1 if hit in positions else None
        self.find_bar.set_count(current, total)

    def clear_find_highlights(self):
        self.editor.tag_remove("findmatch", "1.0", "end")

    def replace_current(self):
        query = self.find_var.get()
        if not query:
            return
        try:
            sel_start, sel_end = self.editor.index("sel.first"), self.editor.index("sel.last")
            selected = self.editor.get(sel_start, sel_end)
            is_match = selected == query if self.match_case_var.get() else selected.lower() == query.lower()
        except tk.TclError:
            is_match = False
        if is_match:
            replacement = self.replace_var.get()
            self.editor.delete(sel_start, sel_end)
            self.editor.insert(sel_start, replacement)
            self.editor.mark_set(tk.INSERT, f"{sel_start}+{len(replacement)}c")
            self.highlight_syntax()
            self._update_line_numbers()
        self.find_next()

    def replace_all(self):
        query = self.find_var.get()
        if not query:
            return
        replacement = self.replace_var.get()
        nocase = not self.match_case_var.get()
        count = 0
        idx = "1.0"
        while True:
            idx = self.editor.search(query, idx, stopindex="end", nocase=nocase)
            if not idx:
                break
            end = f"{idx}+{len(query)}c"
            self.editor.delete(idx, end)
            self.editor.insert(idx, replacement)
            idx = f"{idx}+{len(replacement)}c"
            count += 1
        self.highlight_syntax()
        self._update_line_numbers()
        self.refresh_find_highlights()
        self.set_status(f"Replaced {_plural(count, 'occurrence')}.", "ok" if count else "warning", log=False)

    # ---------- formatting ----------

    def format_sql(self):
        try:
            import sqlparse
        except ImportError:
            ui.show_error(
                self.root, "Formatting needs the sqlparse package",
                "Install it with:  pip install sqlparse",
            )
            return
        try:
            sel_start, sel_end = self.editor.index("sel.first"), self.editor.index("sel.last")
            has_selection = True
        except tk.TclError:
            sel_start, sel_end = "1.0", "end-1c"
            has_selection = False
        text = self.editor.get(sel_start, sel_end)
        if not text.strip():
            self.set_status("Nothing to format.", "muted", log=False)
            return
        formatted = sqlparse.format(text, reindent=True, keyword_case="upper").strip("\n")
        self.editor.delete(sel_start, sel_end)
        self.editor.insert(sel_start, formatted)
        self._after_bulk_edit()
        self.set_status("Formatted selection." if has_selection else "Formatted query.", "ok", log=False)

    # ---------- query execution ----------

    def _selected_sql(self):
        try:
            return self.editor.get("sel.first", "sel.last")
        except tk.TclError:
            return None

    def _require_connection(self):
        if self.backend is not None and self.backend.is_connected:
            return True
        self.set_status("Connect to a database first (Ctrl+K).", "warning", log=False)
        self.open_connect_dialog()
        return self.backend is not None and self.backend.is_connected

    def run_query(self):
        """F5: the selection if there is one, otherwise the whole buffer."""
        if not self._require_connection():
            return
        sql_text = self._selected_sql()
        if sql_text is None:
            sql_text = self.editor.get("1.0", "end-1c")
        self._execute_sql_text(sql_text)

    def run_statement(self):
        """Ctrl+Enter: the selection if there is one, otherwise the statement under the caret."""
        if not self._require_connection():
            return
        sql_text = self._selected_sql()
        if sql_text is None:
            text = self.editor.get("1.0", "end-1c")
            offset = len(self.editor.get("1.0", tk.INSERT))
            sql_text = statement_at(text, offset) or ""
        self._execute_sql_text(sql_text)

    def run_all(self):
        if not self._require_connection():
            return
        self._execute_sql_text(self.editor.get("1.0", "end-1c"))

    def explain_query(self):
        if not self._require_connection():
            return
        sql_text = self._selected_sql()
        if sql_text is None:
            text = self.editor.get("1.0", "end-1c")
            offset = len(self.editor.get("1.0", tk.INSERT))
            sql_text = statement_at(text, offset) or ""
        statements = split_statements(sql_text)
        if not statements:
            self.set_status("Nothing to explain.", "muted", log=False)
            return
        if len(statements) > 1:
            self.set_status("Select just one statement to explain.", "warning", log=False)
            return
        self._execute_sql_text(self.backend.explain_statement(statements[0]), is_explain=True)

    def _execute_sql_text(self, sql_text, is_explain=False):
        if self._query_running:
            self.set_status("A query is already running — stop it first (Ctrl+Break).", "warning", log=False)
            return
        statements = split_statements(sql_text)
        if not statements:
            self.set_status("Nothing to run.", "muted", log=False)
            return

        started = time.monotonic()
        self._cancel_requested = False
        self._set_running(True)

        def work():
            results = []
            for stmt in statements:
                try:
                    columns, rows = self.backend.execute(stmt)
                    results.append((stmt, columns, rows, None))
                except BackendError as exc:
                    results.append((stmt, None, None, exc))
                    break
            return results

        def done(results, error):
            elapsed_ms = (time.monotonic() - started) * 1000
            self._set_running(False)
            self.last_timing = _fmt_elapsed(elapsed_ms)
            self.app._timing_changed(self)
            if error is not None:
                if self._cancel_requested:
                    self.set_status(f"Query cancelled after {_fmt_elapsed(elapsed_ms)}.", "warning")
                else:
                    self.set_status(f"Error after {_fmt_elapsed(elapsed_ms)}: {error}", "error")
                    self.show_messages_tab()
                return
            self._show_batch_results(results, elapsed_ms, record=not is_explain)

        self._run_async(work, done)

    def _set_running(self, running):
        self._query_running = running
        self._run_started = time.monotonic() if running else None
        if running:
            self.set_status("Running…", "busy", log=False)
            self._tick_timer()
        self.app._sync_chrome_if_active(self)

    def _tick_timer(self):
        if not self._query_running or self._closed or self._run_started is None:
            return
        self.last_timing = _fmt_elapsed((time.monotonic() - self._run_started) * 1000)
        self.app._timing_changed(self)
        self.root.after(100, self._tick_timer)

    def cancel_query(self):
        if not self._query_running or self.backend is None:
            return
        self._cancel_requested = True
        self.set_status("Sending cancel request…", "busy", log=False)

        backend = self.backend

        def work():
            return backend.cancel()

        def done(result, error):
            if error is not None:
                self.set_status(f"Cancel request errored: {error}", "error")
            elif not result:
                self.set_status("Cancel request failed — the query may still be running.", "warning")
            elif self._query_running:
                self.set_status("Cancel request sent — waiting for the server to stop the query…", "busy", log=False)

        self._run_async(work, done)

    def _history_connection_label(self):
        if self.connected_server is None:
            return "?"
        server = self.connected_server
        if self.backend is not None and self.backend.label == "SQLite":
            server = os.path.basename(server)
        parts = [server]
        if self.active_database and self.active_database != server:
            parts.append(self.active_database)
        return "/".join(parts)

    def _show_batch_results(self, results, elapsed_ms, record=True):
        self._clear_result_tabs()
        result_count = 0
        total_rows = 0
        had_error = False
        conn_label = self._history_connection_label()

        for stmt, columns, rows, err in results:
            if err is not None:
                had_error = True
                if self._cancel_requested:
                    self._log_message(f"Cancelled: {_sql_snippet(stmt)}", "warning")
                else:
                    self._log_message(f"{err}", "error")
                    self._log_message(f"    in: {_sql_snippet(stmt, 160)}", "muted")
                continue
            if columns is None:
                affected = rows
                self._log_message(f"OK · {_plural(affected, 'row') if affected is not None and affected >= 0 else 'rows'} affected · {_sql_snippet(stmt)}", "ok")
                if record:
                    record_history(stmt, elapsed_ms, affected, conn_label)
                continue
            result_count += 1
            total_rows += len(rows)
            self._add_result_tab(result_count, columns, rows, select=(result_count == 1))
            if record:
                record_history(stmt, elapsed_ms, len(rows), conn_label)

        if result_count == 0:
            self._add_placeholder_result_tab(select=not had_error)
        elif result_count == 1:
            first = self.results_notebook.tabs()[0]
            self.results_notebook.tab(first, text=self.results_notebook.tab(first, "text").replace("Result 1  ·", "Result  ·", 1))

        elapsed = _fmt_elapsed(elapsed_ms)
        if had_error and self._cancel_requested:
            self.set_status(f"Query cancelled after {elapsed}.", "warning", log=False)
            self.show_messages_tab()
        elif had_error:
            first_err = next((str(err) for *_rest, err in results if err is not None), "error")
            self.set_status(f"{_sql_snippet(first_err, 140)}  ·  {elapsed}", "error", log=False)
            self.show_messages_tab()
        elif result_count:
            sets = f" across {_plural(result_count, 'result set')}" if result_count > 1 else ""
            self.set_status(f"{_plural(total_rows, 'row')}{sets}  ·  {elapsed}", "ok")
        else:
            self.set_status(f"Done  ·  {elapsed}", "ok", log=False)
        self._sync_results_footer()

    def _messages_index(self):
        return self.results_notebook.index(self.messages_frame)

    def _clear_result_tabs(self):
        for tab_id in list(self.results_notebook.tabs()):
            widget = self.results_notebook.nametowidget(tab_id)
            if widget is self.messages_frame:
                continue
            self.results_notebook.forget(tab_id)
            widget.destroy()
        self._result_sheets = []
        self._result_meta = []

    def _add_placeholder_result_tab(self, select=True):
        frame = ttk.Frame(self.results_notebook, style="View.TFrame")
        ui.EmptyState(frame, "table", "No results yet",
                      "Run a query to see its rows here.\nF5 runs the selection or everything, Ctrl+Enter the statement at the caret.",
                      ).pack(fill="both", expand=True)
        self.results_notebook.insert(self._messages_index(), frame, text="Results")
        if select:
            self.results_notebook.select(frame)
        return frame

    def _add_result_tab(self, n, columns, rows, select=True):
        th = self.theme
        px = th.px
        frame = ttk.Frame(self.results_notebook, style="View.TFrame")
        sheet = Sheet(
            frame, headers=[], data=[], show_row_index=True, show_top_left=True,
            default_row_index_width=px(52), default_row_height=px(26), default_header_height=px(28),
            empty_horizontal=0, empty_vertical=0, auto_resize_row_index=True,
        )
        sheet.set_options(redraw=False, **th.sheet_options())
        sheet.config(bg=th.pal.view)
        sheet.enable_bindings(
            "single_select", "row_select", "column_select", "arrowkeys",
            "copy", "select_all", "column_width_resize", "double_click_column_resize",
            "rc_select", "rc_popup_menu", "sort_rows", "drag_select",
        )
        sheet.popup_menu_add_command(
            "Copy cells with headers",
            lambda: self._copy_cells_with_headers(sheet),
            index_menu=False, header_menu=False, empty_space_menu=False,
        )
        sheet.pack(fill="both", expand=True)

        self.results_notebook.insert(self._messages_index(), frame, text=f"Result {n}  ·  {_fmt_int(len(rows))}")
        self._result_sheets.append(sheet)
        frame.bind("<Configure>", lambda e, sh=sheet: self._schedule_sheet_scrollbar_sync(sh), add="+")
        self._result_meta.append({"rows": len(rows), "shown": min(len(rows), RESULT_ROW_CAP), "columns": len(columns)})
        self._populate_sheet(sheet, columns, rows)
        if select:
            self.results_notebook.select(frame)
        return sheet

    def _populate_sheet(self, sheet, columns, rows):
        th = self.theme
        shown_rows = rows[:RESULT_ROW_CAP]
        cell_font = th.fonts.mono_small
        header_font = th.fonts.mono_small_bold
        max_width_px = th.px(360)
        min_width_px = th.px(56)
        padding_px = th.px(22)
        sample = shown_rows[:500]

        col_widths = []
        for i, col in enumerate(columns):
            header_w = header_font.measure(str(col))
            cell_w = max(
                (cell_font.measure("NULL" if r[i] is None else str(r[i])) for r in sample),
                default=0,
            )
            width = max(min_width_px, min(max(header_w, cell_w) + padding_px, max_width_px))
            col_widths.append(width)

        display_rows = []
        null_cells = []
        for r_idx, row in enumerate(shown_rows):
            display_row = []
            for c_idx, v in enumerate(row):
                if v is None:
                    display_row.append("NULL")
                    null_cells.append((r_idx, c_idx))
                else:
                    display_row.append(str(v))
            display_rows.append(display_row)

        sheet.headers(list(columns))
        sheet.set_sheet_data(display_rows, reset_highlights=True)
        sheet.set_column_widths(col_widths)
        sheet._np_null_cells = null_cells
        if null_cells:
            sheet.highlight_cells(cells=null_cells, fg=th.pal.grid_null, redraw=False)
        sheet.redraw()
        self._schedule_sheet_scrollbar_sync(sheet)

    def _schedule_sheet_scrollbar_sync(self, sheet):
        pending = getattr(sheet, "_np_sync_after", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        sheet._np_sync_after = self.root.after(60, lambda: self._sync_sheet_scrollbars(sheet))

    def _sync_sheet_scrollbars(self, sheet):
        """tksheet always shows both scrollbars; hide the ones with nothing to scroll."""
        sheet._np_sync_after = None
        try:
            if not sheet.winfo_exists():
                return
            x0, x1 = (float(v) for v in sheet.MT.xview())
            y0, y1 = (float(v) for v in sheet.MT.yview())
        except (tk.TclError, ValueError, AttributeError):
            return
        try:
            if x0 <= 0.0 and x1 >= 1.0:
                sheet.hide("x_scrollbar")
            else:
                sheet.show("x_scrollbar")
            if y0 <= 0.0 and y1 >= 1.0:
                sheet.hide("y_scrollbar")
            else:
                sheet.show("y_scrollbar")
        except (tk.TclError, AttributeError):
            pass

    def _current_sheet(self):
        sel = self.results_notebook.select()
        if not sel:
            return None
        try:
            idx = self.results_notebook.index(sel)
        except tk.TclError:
            return None
        if idx < len(self._result_sheets):
            return self._result_sheets[idx]
        return None

    def _sync_results_footer(self):
        try:
            sheet = self._current_sheet()
        except tk.TclError:
            return
        if sheet is None:
            self.footer_label.configure(text="")
            self.export_btn.configure(state="disabled")
            return
        idx = self._result_sheets.index(sheet)
        meta = self._result_meta[idx] if idx < len(self._result_meta) else None
        if meta is None:
            self.footer_label.configure(text="")
            self.export_btn.configure(state="disabled")
            return
        parts = [_plural(meta["rows"], "row"), _plural(meta["columns"], "column")]
        if meta["rows"] > meta["shown"]:
            parts.append(f"showing the first {_fmt_int(meta['shown'])}")
        if self.last_timing:
            parts.append(self.last_timing)
        self.footer_label.configure(text="  ·  ".join(parts))
        self.export_btn.configure(state="normal")

    def export_csv(self):
        sheet = self._current_sheet()
        if sheet is None:
            self.set_status("Run a query that returns rows first.", "warning", log=False)
            return
        headers = sheet.headers()
        data = sheet.get_sheet_data()
        if not headers:
            self.set_status("Run a query that returns rows first.", "warning", log=False)
            return

        base = os.path.splitext(self.title)[0] or "results"
        path = ui.pick_save_file(self.root, "Export results to CSV", CSV_FILETYPES, initialfile=f"{base}.csv",
                                 defaultextension=".csv")
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(data)
        except Exception as exc:
            ui.show_error(self.root, "Export failed", str(exc))
            return

        self.set_status(f"Exported {_plural(len(data), 'row')} to {path}", "ok")

    # ---------- async helper ----------

    def _run_async(self, work_fn, done_fn):
        def runner():
            try:
                result = work_fn()
                self._work_queue.put((done_fn, result, None))
            except BackendError as exc:
                self._work_queue.put((done_fn, None, exc))
            except Exception as exc:
                self._work_queue.put((done_fn, None, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                done_fn, result, error = self._work_queue.get_nowait()
                done_fn(result, error)
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(100, self._poll_queue)

    # ---------- lifecycle ----------

    def reset_blank(self):
        """Used when closing the last remaining tab — reset it instead of
        leaving the app with zero tabs."""
        if self.backend is not None:
            self.backend.disconnect()
        self.backend = None
        self.connected_server = None
        self.databases = []
        self.db_schema_cache = {}
        self.active_database = None
        self.schema = None
        self.populate_schema_tree()
        self.editor.delete("1.0", "end")
        self.editor.edit_reset()
        self.current_file = None
        self.set_title(f"{self.TITLE_PREFIX} {self.index}")
        self._mark_clean()
        self._clear_result_tabs()
        self._add_placeholder_result_tab()
        self.last_timing = ""
        self._after_bulk_edit()
        self.app._sync_chrome_if_active(self)
        self.set_status("Ready.", "info", log=False)

    def teardown(self):
        self._closed = True
        self._hide_tooltip()
        self._end_drag_ghost()
        self.autocomplete.hide()
        if self.backend is not None:
            self.backend.disconnect()

    def session_state(self):
        """Snapshot for session-restore: buffer content, file association,
        and (password-free) connection params if currently connected."""
        connection = None
        if self.backend is not None and self.backend.is_connected:
            engine = next((k for k, v in BACKENDS.items() if isinstance(self.backend, v)), None)
            connection = {
                "engine": engine,
                "server": self.connected_server,
                "database": self.active_database,
                "auth_mode": self._last_auth_mode,
                "port": self._last_port,
                "encrypt": self._last_encrypt,
                "trust_cert": self._last_trust_cert,
            }
        return {
            "content": self.editor.get("1.0", "end-1c"),
            "current_file": self.current_file,
            "title": self.title if self.current_file is None else None,
            "connection": connection,
        }


class NotepaDBApp:
    def __init__(self, root):
        self.root = root
        self.tabs = []
        self._tab_for_frame = {}
        self._tab_counter = 0
        self._menus = []
        self._icon_buttons = []
        self._history_dialog = None
        self._pressed_close = None
        self._fullscreen = False

        cfg = load_config()
        self.theme = T.Theme(root)
        ui.install(self.theme)
        mode = cfg.get("theme_mode") or ("dark" if cfg.get("dark_mode") else "system")
        if mode not in T.Theme.MODES:
            mode = "system"
        self.theme.set_mode(mode, notify=False)
        self.theme_mode_var = tk.StringVar(value=mode)
        self.show_line_numbers_var = tk.BooleanVar(value=cfg.get("show_line_numbers", True))
        self.word_wrap_var = tk.BooleanVar(value=cfg.get("word_wrap", False))
        self.restore_session_var = tk.BooleanVar(value=cfg.get("restore_session", False))
        self.show_sidebar_var = tk.BooleanVar(value=cfg.get("show_sidebar", True))
        self.sidebar_width = int(cfg.get("sidebar_width") or self.theme.px(290))
        size = cfg.get("editor_font_size")
        if size:
            self.theme.fonts.set_editor_size(size)

        px = self.theme.px
        self.root.title(APP_NAME)
        self.root.minsize(px(900), px(560))
        try:
            self.root.iconphoto(True, *self.theme.app_icons())
        except tk.TclError:
            pass

        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._build_notebook()
        self.palette = ui.CommandPalette(root, self._palette_items)
        self._bind_shortcuts()

        self._restore_or_new_tab()
        self._apply_chrome_theme()
        self.theme.add_listener(self._on_theme_changed)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30000, self._autosave_tick)

    def startup(self):
        """Things that need the main window on screen first (modal prompts)."""
        self._offer_crash_recovery()
        tab = self.active_tab
        if tab is not None:
            tab.editor.focus_set()

    @property
    def active_tab(self):
        if not self.tabs:
            return None
        try:
            sel = self.notebook.select()
        except tk.TclError:
            return None
        if not sel:
            return None
        frame = self.notebook.nametowidget(sel)
        return self._tab_for_frame.get(frame)

    def _dispatch(self, method_name, *args):
        tab = self.active_tab
        if tab is not None:
            getattr(tab, method_name)(*args)

    # ---------- status plumbing ----------

    def set_status(self, tab, message, kind="info", log=True):
        tab.set_status(message, kind, log)

    def _status_changed(self, tab):
        if self.active_tab is tab:
            text, kind = tab.last_status
            self.statusbar.set_message(text, kind)
            self.statusbar.set_busy(tab._query_running or kind == "busy")

    def _timing_changed(self, tab):
        if self.active_tab is tab:
            self.statusbar.set_timing(tab.last_timing)
            tab._sync_results_footer()

    def _update_cursor_position(self, tab):
        if self.active_tab is not tab:
            return
        try:
            line, col = tab.editor.index("insert").split(".")
            self.statusbar.set_position(int(line), int(col) + 1)
        except (tk.TclError, ValueError):
            pass

    def _sync_chrome_if_active(self, tab):
        if self.active_tab is tab:
            self._sync_chrome()

    def _sync_chrome(self):
        tab = self.active_tab
        if tab is None:
            return
        running = tab._query_running
        self.run_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        text, state = tab.connection_label()
        self.statusbar.set_connection(text, state)
        short = tab.short_connection_label()
        self.connect_btn.configure(text=f" {short}" if short else " Connect")
        self.statusbar.set_message(*tab.last_status)
        self.statusbar.set_busy(running or tab.last_status[1] == "busy")
        self.statusbar.set_timing(tab.last_timing)
        self._update_cursor_position(tab)
        self._update_title()

    def _update_title(self):
        tab = self.active_tab
        if tab is None:
            self.root.title(APP_NAME)
            return
        self.root.title(f"{tab.title}{' •' if tab.dirty else ''} — {APP_NAME}")

    # ---------- options ----------

    def set_theme_mode(self, mode):
        self.theme_mode_var.set(mode)
        update_config(theme_mode=mode)
        self.theme.set_mode(mode)

    def toggle_theme(self):
        self.set_theme_mode("light" if self.theme.dark else "dark")

    def _on_theme_changed(self, pal):
        self._apply_chrome_theme()
        self._refresh_toolbar_icons()
        for tab in self.tabs:
            tab.apply_theme(pal)
        self._sync_chrome()

    def toggle_line_numbers(self):
        update_config(show_line_numbers=self.show_line_numbers_var.get())
        for tab in self.tabs:
            tab.apply_line_numbers_visibility(self.show_line_numbers_var.get())

    def toggle_word_wrap(self):
        update_config(word_wrap=self.word_wrap_var.get())
        for tab in self.tabs:
            tab.apply_word_wrap(self.word_wrap_var.get())

    def toggle_restore_session(self):
        update_config(restore_session=self.restore_session_var.get())

    def toggle_sidebar(self, from_menu=False):
        if not from_menu:
            self.show_sidebar_var.set(not self.show_sidebar_var.get())
        update_config(show_sidebar=self.show_sidebar_var.get())
        for tab in self.tabs:
            tab.apply_sidebar_visibility(self.show_sidebar_var.get())

    def zoom(self, delta):
        size = self.theme.fonts.set_editor_size(self.theme.fonts.editor_size() + delta)
        update_config(editor_font_size=size)
        for tab in self.tabs:
            tab._update_line_numbers()
        tab = self.active_tab
        if tab is not None:
            tab.set_status(f"Editor font size {size} pt", "muted", log=False)

    def zoom_reset(self):
        self.theme.fonts.set_editor_size(self.theme.fonts.mono_size)
        update_config(editor_font_size=None)
        for tab in self.tabs:
            tab._update_line_numbers()

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        try:
            self.root.attributes("-fullscreen", self._fullscreen)
        except tk.TclError:
            pass

    def open_palette(self):
        self.palette.open()

    def open_history_dialog(self):
        if self._history_dialog is not None and self._history_dialog.winfo_exists():
            self._history_dialog.refresh()
            self._history_dialog.present()
            return
        self._history_dialog = HistoryDialog(self)
        self._history_dialog.present()

    def show_shortcuts(self):
        ShortcutsDialog(self.root, self.theme).show()

    def show_about(self):
        AboutDialog(self.root, self.theme).show()

    # ---------- theme plumbing for classic Tk widgets ----------

    def _apply_chrome_theme(self):
        pal = self.theme.pal
        try:
            self.root.configure(background=pal.window)
        except tk.TclError:
            pass
        for menu in self._menus:
            self.theme.style_menu(menu)
        if self._menus:
            try:
                self._menus[0].configure(background=pal.window, activebackground=pal.hover,
                                         activeforeground=pal.fg, borderwidth=0)
            except tk.TclError:
                pass

    def _refresh_toolbar_icons(self):
        th = self.theme
        for btn, name, role in self._icon_buttons:
            try:
                btn.configure(image=th.icon_states(name, role=role))
            except tk.TclError:
                pass
        self.theme_btn.configure(image=th.icon_states("sun" if th.dark else "moon"))

    # ---------- UI construction ----------

    def _build_menu(self):
        menubar = tk.Menu(self.root, tearoff=0)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New tab", command=self.new_tab, accelerator="Ctrl+T")
        file_menu.add_command(label="Open…", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Save", command=lambda: self._dispatch("save_file"), accelerator="Ctrl+S")
        file_menu.add_command(label="Save as…", command=lambda: self._dispatch("save_file_as"), accelerator="Ctrl+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Close tab", command=self.close_tab, accelerator="Ctrl+W")
        file_menu.add_command(label="Close other tabs", command=lambda: self.close_other_tabs(self.active_tab))
        file_menu.add_separator()
        file_menu.add_checkbutton(label="Restore tabs on startup", variable=self.restore_session_var,
                                  command=self.toggle_restore_session)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Undo", command=lambda: self._dispatch("undo"), accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=lambda: self._dispatch("redo"), accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", command=lambda: self._editor_event("<<Cut>>"), accelerator="Ctrl+X")
        edit_menu.add_command(label="Copy", command=lambda: self._editor_event("<<Copy>>"), accelerator="Ctrl+C")
        edit_menu.add_command(label="Paste", command=lambda: self._editor_event("<<Paste>>"), accelerator="Ctrl+V")
        edit_menu.add_command(label="Select all", command=lambda: self._dispatch("select_all"), accelerator="Ctrl+A")
        edit_menu.add_separator()
        edit_menu.add_command(label="Find…", command=lambda: self._dispatch("open_find_bar"), accelerator="Ctrl+F")
        edit_menu.add_command(label="Find next", command=lambda: self._dispatch("find_next"), accelerator="F3")
        edit_menu.add_command(label="Find previous", command=lambda: self._dispatch("find_previous"), accelerator="Shift+F3")
        edit_menu.add_command(label="Replace…", command=lambda: self._dispatch("open_replace_bar"), accelerator="Ctrl+H")
        edit_menu.add_separator()
        edit_menu.add_command(label="Toggle comment", command=lambda: self._dispatch("toggle_comment"), accelerator="Ctrl+/")
        edit_menu.add_command(label="Format SQL", command=lambda: self._dispatch("format_sql"), accelerator="Ctrl+Shift+F")
        edit_menu.add_command(label="Go to line…", command=lambda: self._dispatch("go_to_line"), accelerator="Ctrl+G")
        menubar.add_cascade(label="Edit", menu=edit_menu)

        conn_menu = tk.Menu(menubar, tearoff=0)
        conn_menu.add_command(label="Connect…", command=lambda: self._dispatch("open_connect_dialog"), accelerator="Ctrl+K")
        conn_menu.add_command(label="Disconnect", command=lambda: self._dispatch("disconnect"))
        conn_menu.add_separator()
        conn_menu.add_command(label="Refresh schema", command=lambda: self._dispatch("refresh_schema"), accelerator="Ctrl+Shift+R")
        menubar.add_cascade(label="Connection", menu=conn_menu)

        query_menu = tk.Menu(menubar, tearoff=0)
        query_menu.add_command(label="Run selection or all", command=lambda: self._dispatch("run_query"), accelerator="F5")
        query_menu.add_command(label="Run statement at caret", command=lambda: self._dispatch("run_statement"), accelerator="Ctrl+Enter")
        query_menu.add_command(label="Run everything", command=lambda: self._dispatch("run_all"), accelerator="Ctrl+Shift+Enter")
        query_menu.add_command(label="Stop", command=lambda: self._dispatch("cancel_query"), accelerator="Ctrl+Break")
        query_menu.add_separator()
        query_menu.add_command(label="Explain plan", command=lambda: self._dispatch("explain_query"), accelerator="Ctrl+L")
        query_menu.add_command(label="History…", command=self.open_history_dialog, accelerator="Ctrl+Shift+H")
        query_menu.add_separator()
        query_menu.add_command(label="Export results to CSV…", command=lambda: self._dispatch("export_csv"))
        menubar.add_cascade(label="Query", menu=query_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_checkbutton(label="Schema sidebar", variable=self.show_sidebar_var,
                                  command=lambda: self.toggle_sidebar(from_menu=True), accelerator="Ctrl+B")
        view_menu.add_checkbutton(label="Line numbers", variable=self.show_line_numbers_var, command=self.toggle_line_numbers)
        view_menu.add_checkbutton(label="Word wrap", variable=self.word_wrap_var, command=self.toggle_word_wrap)
        view_menu.add_separator()
        theme_menu = tk.Menu(view_menu, tearoff=0)
        for label, mode in (("Follow system", "system"), ("Light", "light"), ("Dark", "dark")):
            theme_menu.add_radiobutton(label=label, value=mode, variable=self.theme_mode_var,
                                       command=lambda m=mode: self.set_theme_mode(m))
        view_menu.add_cascade(label="Theme", menu=theme_menu)
        view_menu.add_separator()
        view_menu.add_command(label="Zoom in", command=lambda: self.zoom(1), accelerator="Ctrl+=")
        view_menu.add_command(label="Zoom out", command=lambda: self.zoom(-1), accelerator="Ctrl+-")
        view_menu.add_command(label="Reset zoom", command=self.zoom_reset, accelerator="Ctrl+0")
        view_menu.add_separator()
        view_menu.add_command(label="Full screen", command=self.toggle_fullscreen, accelerator="F11")
        view_menu.add_command(label="Command palette…", command=self.open_palette, accelerator="Ctrl+Shift+P")
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Keyboard shortcuts", command=self.show_shortcuts, accelerator="F1")
        help_menu.add_command(label="Website", command=lambda: webbrowser.open(WEBSITE_URL))
        help_menu.add_command(label="Report an issue", command=lambda: webbrowser.open(ISSUES_URL))
        help_menu.add_separator()
        help_menu.add_command(label=f"About {APP_NAME}", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self._menus = [menubar, file_menu, edit_menu, conn_menu, query_menu, view_menu, theme_menu, help_menu]
        self.root.config(menu=menubar)

    def _editor_event(self, sequence):
        tab = self.active_tab
        if tab is not None:
            tab.editor.event_generate(sequence)
            tab._after_bulk_edit()

    def _bind_shortcuts(self):
        root = self.root

        def bind(seq, fn, *, swallow=False):
            def handler(event=None):
                fn()
                return "break" if swallow else None
            try:
                root.bind(seq, handler)
            except tk.TclError:
                pass

        bind("<Control-t>", self.new_tab)
        bind("<Control-n>", self.new_tab)
        bind("<Control-w>", self.close_tab)
        bind("<Control-o>", self.open_file)
        bind("<Control-s>", lambda: self._dispatch("save_file"))
        bind("<Control-Shift-S>", lambda: self._dispatch("save_file_as"))
        bind("<Control-q>", self._on_close)
        bind("<Control-k>", lambda: self._dispatch("open_connect_dialog"))
        bind("<Control-Shift-R>", lambda: self._dispatch("refresh_schema"))
        bind("<F5>", lambda: self._dispatch("run_query"))
        bind("<Control-Return>", lambda: self._dispatch("run_statement"))
        bind("<Control-Shift-Return>", lambda: self._dispatch("run_all"))
        bind("<Control-Cancel>", lambda: self._dispatch("cancel_query"))
        bind("<Control-Break>", lambda: self._dispatch("cancel_query"))
        bind("<Control-Pause>", lambda: self._dispatch("cancel_query"))
        bind("<Control-l>", lambda: self._dispatch("explain_query"))
        bind("<Control-Shift-F>", lambda: self._dispatch("format_sql"))
        bind("<Control-Shift-H>", self.open_history_dialog)
        bind("<Control-f>", lambda: self._dispatch("open_find_bar"))
        bind("<Control-h>", lambda: self._dispatch("open_replace_bar"))
        bind("<F3>", lambda: self._dispatch("find_next"))
        bind("<Shift-F3>", lambda: self._dispatch("find_previous"))
        bind("<Control-slash>", lambda: self._dispatch("toggle_comment"))
        bind("<Control-g>", lambda: self._dispatch("go_to_line"))
        bind("<Control-Shift-P>", self.open_palette)
        bind("<Control-b>", self.toggle_sidebar)
        bind("<Control-equal>", lambda: self.zoom(1))
        bind("<Control-plus>", lambda: self.zoom(1))
        bind("<Control-KP_Add>", lambda: self.zoom(1))
        bind("<Control-minus>", lambda: self.zoom(-1))
        bind("<Control-KP_Subtract>", lambda: self.zoom(-1))
        bind("<Control-0>", self.zoom_reset)
        bind("<Control-KP_0>", self.zoom_reset)
        bind("<F11>", self.toggle_fullscreen)
        bind("<F1>", self.show_shortcuts)
        bind("<Control-Tab>", lambda: self.next_tab(1), swallow=True)
        bind("<Control-Shift-Tab>", lambda: self.next_tab(-1), swallow=True)
        bind("<Control-ISO_Left_Tab>", lambda: self.next_tab(-1), swallow=True)
        bind("<Control-Shift-ISO_Left_Tab>", lambda: self.next_tab(-1), swallow=True)
        bind("<Control-Next>", lambda: self.next_tab(1), swallow=True)
        bind("<Control-Prior>", lambda: self.next_tab(-1), swallow=True)

        # Ctrl+A selects all in every entry field, the way the rest of the desktop does.
        def entry_select_all(event):
            try:
                event.widget.select_range(0, "end")
                event.widget.icursor("end")
            except (AttributeError, tk.TclError):
                return None
            return "break"

        for cls in ("TEntry", "TCombobox", "Entry"):
            root.bind_class(cls, "<Control-a>", entry_select_all)

    def _build_toolbar(self):
        th = self.theme
        px = th.px
        bar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(px(10), px(6), px(10), px(4)))
        bar.pack(side="top", fill="x")
        self.toolbar = bar

        def tool(name, text, command, tip, style="Toolbutton", role="fg", side="left", padx=(0, 0), **kw):
            btn = ttk.Button(
                bar, text=text, image=th.icon_states(name, role=role), compound="left" if text else "none",
                style=style, command=command, **kw,
            )
            btn.pack(side=side, padx=padx)
            self._icon_buttons.append((btn, name, role))
            ui.Tooltip(btn, tip)
            return btn

        def separator():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=px(8), pady=px(6))

        self.run_btn = tool("play", " Run", lambda: self._dispatch("run_query"),
                            "Run the selection, or everything (F5)\nCtrl+Enter runs just the statement at the caret",
                            style="Accent.TButton", role="accent_fg")
        self.stop_btn = tool("stop", " Stop", lambda: self._dispatch("cancel_query"),
                             "Stop the running query (Ctrl+Break)", padx=(px(6), 0), state="disabled", style="Toolbutton")
        separator()
        self.connect_btn = tool("plug", " Connect", lambda: self._dispatch("open_connect_dialog"),
                                "Connect to a database (Ctrl+K)")
        tool("refresh", "", lambda: self._dispatch("refresh_schema"), "Refresh schema (Ctrl+Shift+R)", style="Icon.Toolbutton")
        separator()
        tool("tab", "", self.new_tab, "New tab (Ctrl+T)", style="Icon.Toolbutton")
        tool("folder", "", self.open_file, "Open a SQL file (Ctrl+O)", style="Icon.Toolbutton")
        tool("save", "", lambda: self._dispatch("save_file"), "Save (Ctrl+S)", style="Icon.Toolbutton")
        separator()
        tool("format", "", lambda: self._dispatch("format_sql"), "Format SQL (Ctrl+Shift+F)", style="Icon.Toolbutton")
        tool("history", "", self.open_history_dialog, "Query history (Ctrl+Shift+H)", style="Icon.Toolbutton")

        self.theme_btn = ttk.Button(bar, image=th.icon_states("sun" if th.dark else "moon"), style="Icon.Toolbutton",
                                    command=self.toggle_theme)
        self.theme_btn.pack(side="right")
        ui.Tooltip(self.theme_btn, lambda: "Switch to the light theme" if self.theme.dark else "Switch to the dark theme")
        tool("terminal", "", self.open_palette, "Command palette (Ctrl+Shift+P)", style="Icon.Toolbutton", side="right")
        tool("sidebar", "", self.toggle_sidebar, "Show or hide the schema sidebar (Ctrl+B)", style="Icon.Toolbutton", side="right")

    def _build_statusbar(self):
        self.statusbar = ui.StatusBar(self.root, on_connection_click=lambda: self._dispatch("open_connect_dialog"))
        self.statusbar.pack(side="bottom", fill="x")

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root, style="Main.TNotebook")
        self.notebook.pack(side="top", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.bind("<ButtonPress-1>", self._on_tab_press, add="+")
        self.notebook.bind("<ButtonRelease-1>", self._on_tab_release, add="+")
        self.notebook.bind("<Button-2>", self._on_tab_middle_click)
        self.notebook.bind("<Button-3>", self._on_tab_right_click)
        self.notebook.bind("<Double-Button-1>", self._on_tab_double_click)

    # ---------- tab management ----------

    def _tab_at(self, x, y):
        try:
            idx = self.notebook.index(f"@{x},{y}")
        except tk.TclError:
            return None, None
        frame = self.notebook.nametowidget(self.notebook.tabs()[idx])
        return idx, self._tab_for_frame.get(frame)

    def _on_tab_changed(self, event=None):
        self._sync_chrome()
        tab = self.active_tab
        if tab is not None:
            tab.editor.focus_set()

    def _on_tab_press(self, event):
        element = self.notebook.identify(event.x, event.y) or ""
        if "close" in element:
            idx, _tab = self._tab_at(event.x, event.y)
            if idx is not None:
                self._pressed_close = idx
                self.notebook.state(["pressed"])
                return "break"
        self._pressed_close = None
        return None

    def _on_tab_release(self, event):
        if self._pressed_close is None:
            return None
        self.notebook.state(["!pressed"])
        element = self.notebook.identify(event.x, event.y) or ""
        idx, tab = self._tab_at(event.x, event.y)
        pressed = self._pressed_close
        self._pressed_close = None
        if "close" in element and idx == pressed and tab is not None:
            self.close_tab(tab)
        return "break"

    def _on_tab_middle_click(self, event):
        _idx, tab = self._tab_at(event.x, event.y)
        if tab is not None:
            self.close_tab(tab)
            return "break"
        return None

    def _on_tab_double_click(self, event):
        element = self.notebook.identify(event.x, event.y) or ""
        _idx, tab = self._tab_at(event.x, event.y)
        if tab is None and not element:
            self.new_tab()
            return "break"
        return None

    def _on_tab_right_click(self, event):
        _idx, tab = self._tab_at(event.x, event.y)
        menu = tk.Menu(self.root, tearoff=0)
        if tab is not None:
            menu.add_command(label="Close tab", command=lambda: self.close_tab(tab), accelerator="Ctrl+W")
            menu.add_command(label="Close other tabs", command=lambda: self.close_other_tabs(tab),
                             state="normal" if len(self.tabs) > 1 else "disabled")
            menu.add_command(label="Close all tabs", command=self.close_all_tabs)
            menu.add_separator()
        menu.add_command(label="New tab", command=self.new_tab, accelerator="Ctrl+T")
        self.theme.style_menu(menu)
        menu.tk_popup(event.x_root, event.y_root)

    def new_tab(self):
        self._tab_counter += 1
        tab = QueryTab(self, self._tab_counter)
        self.notebook.add(tab.frame, text=tab.title)
        self._tab_for_frame[tab.frame] = tab
        self.tabs.append(tab)
        self.notebook.select(tab.frame)
        tab.editor.focus_set()
        self._sync_chrome()
        return tab

    def next_tab(self, delta):
        if len(self.tabs) < 2:
            return
        tabs = self.notebook.tabs()
        try:
            cur = tabs.index(self.notebook.select())
        except ValueError:
            return
        self.notebook.select(tabs[(cur + delta) % len(tabs)])

    def open_file(self):
        tab = self.active_tab
        initialdir = os.path.dirname(tab.current_file) if tab is not None and tab.current_file else None
        path = ui.pick_open_file(self.root, "Open a SQL file", SQL_FILETYPES, initialdir=initialdir)
        if not path:
            return
        # Reuse an empty, untouched tab instead of opening next to it.
        if tab is not None and not tab.dirty and tab.current_file is None and not tab.editor.get("1.0", "end-1c").strip():
            target = tab
        else:
            target = self.new_tab()
        try:
            target.load_file(path)
        except OSError as exc:
            ui.show_error(self.root, "Couldn't open file", str(exc))

    def close_tab(self, tab=None):
        tab = tab or self.active_tab
        if tab is None:
            return
        if not self._confirm_close(tab):
            return
        if len(self.tabs) == 1:
            tab.reset_blank()
            return
        tab.teardown()
        self.notebook.forget(tab.frame)
        del self._tab_for_frame[tab.frame]
        self.tabs.remove(tab)
        tab.frame.destroy()
        self._sync_chrome()

    def close_other_tabs(self, keep):
        for tab in list(self.tabs):
            if tab is not keep:
                if not self._confirm_close(tab):
                    return
                tab.teardown()
                self.notebook.forget(tab.frame)
                del self._tab_for_frame[tab.frame]
                self.tabs.remove(tab)
                tab.frame.destroy()
        self._sync_chrome()

    def close_all_tabs(self):
        for tab in list(self.tabs):
            if not self._confirm_close(tab):
                return
            self.close_tab(tab)

    def _confirm_close(self, tab):
        """True if it's OK to proceed closing/discarding `tab` — prompts to
        save first if it has unsaved changes. False means the caller should
        abort (either the user hit Cancel, or a Save-As they triggered was
        itself cancelled)."""
        if not tab.dirty:
            return True
        self.notebook.select(tab.frame)
        answer = ui.ask_save_changes(self.root, tab.title)
        if answer == "cancel":
            return False
        if answer == "save":
            tab.save_file()
            if tab.dirty:
                return False
        return True

    # ---------- command palette ----------

    def _palette_items(self):
        d = self._dispatch
        items = [
            ("Run selection or everything", "F5", lambda: d("run_query"), "execute query"),
            ("Run statement at caret", "Ctrl+Enter", lambda: d("run_statement"), "execute"),
            ("Run everything", "Ctrl+Shift+Enter", lambda: d("run_all"), "execute all"),
            ("Stop query", "Ctrl+Break", lambda: d("cancel_query"), "cancel"),
            ("Explain plan", "Ctrl+L", lambda: d("explain_query"), "query plan"),
            ("Connect to a database…", "Ctrl+K", lambda: d("open_connect_dialog"), "connection server"),
            ("Disconnect", "", lambda: d("disconnect"), "connection"),
            ("Refresh schema", "Ctrl+Shift+R", lambda: d("refresh_schema"), "reload tables"),
            ("New tab", "Ctrl+T", self.new_tab, ""),
            ("Close tab", "Ctrl+W", self.close_tab, ""),
            ("Open SQL file…", "Ctrl+O", self.open_file, "load"),
            ("Save", "Ctrl+S", lambda: d("save_file"), "write file"),
            ("Save as…", "Ctrl+Shift+S", lambda: d("save_file_as"), "write file"),
            ("Find", "Ctrl+F", lambda: d("open_find_bar"), "search"),
            ("Replace", "Ctrl+H", lambda: d("open_replace_bar"), "search substitute"),
            ("Go to line…", "Ctrl+G", lambda: d("go_to_line"), "jump"),
            ("Toggle comment", "Ctrl+/", lambda: d("toggle_comment"), "comment lines"),
            ("Format SQL", "Ctrl+Shift+F", lambda: d("format_sql"), "pretty print beautify"),
            ("Query history…", "Ctrl+Shift+H", self.open_history_dialog, "recent queries"),
            ("Export results to CSV…", "", lambda: d("export_csv"), "save grid"),
            ("Toggle schema sidebar", "Ctrl+B", self.toggle_sidebar, "explorer panel"),
            ("Toggle line numbers", "", lambda: (self.show_line_numbers_var.set(not self.show_line_numbers_var.get()), self.toggle_line_numbers()), "gutter"),
            ("Toggle word wrap", "", lambda: (self.word_wrap_var.set(not self.word_wrap_var.get()), self.toggle_word_wrap()), "editor"),
            ("Theme: follow system", "", lambda: self.set_theme_mode("system"), "appearance dark light"),
            ("Theme: light", "", lambda: self.set_theme_mode("light"), "appearance"),
            ("Theme: dark", "", lambda: self.set_theme_mode("dark"), "appearance"),
            ("Zoom in", "Ctrl+=", lambda: self.zoom(1), "font size bigger"),
            ("Zoom out", "Ctrl+-", lambda: self.zoom(-1), "font size smaller"),
            ("Reset zoom", "Ctrl+0", self.zoom_reset, "font size"),
            ("Full screen", "F11", self.toggle_fullscreen, "window"),
            ("Keyboard shortcuts", "F1", self.show_shortcuts, "help keys"),
            (f"About {APP_NAME}", "", self.show_about, "version"),
        ]
        tab = self.active_tab
        if tab is not None and tab.schema:
            for table_key in sorted(tab.schema.keys(), key=str.lower):
                items.append((f"Table: {table_key}", "select top 1000 rows",
                              lambda k=table_key: tab._select_top_rows(k), "open browse"))
        if tab is not None and tab.databases and len(tab.databases) > 1:
            for db in sorted(tab.databases, key=str.lower):
                if db != tab.active_database:
                    items.append((f"Use database: {db}", "", lambda n=db: tab._set_active_database(n), "switch"))
        return items

    # ---------- closing ----------

    def _is_zoomed(self):
        try:
            if self.root.state() == "zoomed":
                return True
        except tk.TclError:
            pass
        try:
            return bool(self.root.attributes("-zoomed"))
        except tk.TclError:
            return False

    def _on_close(self):
        for tab in self.tabs:
            if not self._confirm_close(tab):
                return
        save_session([tab.session_state() for tab in self.tabs])
        values = {"window_zoomed": self._is_zoomed()}
        if not values["window_zoomed"] and not self._fullscreen:
            values["window_geometry"] = self.root.geometry()
        # Prefer the active tab's sidebar width; fall back to any tab whose
        # pane has actually been laid out (a tab opened a moment ago may not be).
        widths = [t.sidebar_width() for t in ([self.active_tab] if self.active_tab else []) + self.tabs]
        widths = [w for w in widths if w and w > 40]
        if widths:
            values["sidebar_width"] = widths[0]
        update_config(**values)
        for tab in self.tabs:
            if tab.backend is not None:
                tab.backend.disconnect()
        self._clear_autosave()
        self.root.destroy()

    # ---------- session restore / crash recovery ----------

    def _restore_or_new_tab(self):
        if self.restore_session_var.get():
            for state in load_session():
                self._open_tab_from_session(state)
        if not self.tabs:
            self.new_tab()

    def _open_tab_from_session(self, state):
        tab = self.new_tab()
        content = state.get("content") or ""
        if content:
            tab.editor.delete("1.0", "end")
            tab.editor.insert("1.0", content)
            tab.editor.edit_reset()
            tab._after_bulk_edit()

        current_file = state.get("current_file")
        if current_file and os.path.isfile(current_file):
            tab.current_file = current_file
            tab.set_title(os.path.basename(current_file))
        elif state.get("title"):
            tab.set_title(state["title"])
        tab._mark_clean()

        conn = state.get("connection")
        if not conn:
            return
        # Passwords are never persisted, so only auto-reconnect when none is
        # needed — Windows auth or a local SQLite file. Anything else just
        # gets a reminder; the user re-enters credentials via Connect.
        if conn.get("engine") == "sqlite" or conn.get("auth_mode") == "windows":
            params = dict(conn, username="", password="")
            tab._connect_with(params)
        else:
            label = conn.get("server", "?")
            if conn.get("database"):
                label += f"/{conn['database']}"
            tab.set_status(f"Session restored — reconnect to {label} with Ctrl+K (passwords aren't saved).", "warning")

    def _offer_crash_recovery(self):
        files = _list_autosave_files()
        if not files:
            return
        recover = ui.ask_yes_no(
            self.root, "Recover unsaved work?",
            f"{APP_NAME} didn't close cleanly last time. {_plural(len(files), 'tab')} with unsaved "
            "changes can be restored.",
            yes="Recover", no="Discard",
        )
        if not recover:
            for path in files:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            tab = self.new_tab()
            tab.editor.delete("1.0", "end")
            tab.editor.insert("1.0", content)
            tab.editor.edit_modified(True)
            tab._on_modified_flag(None)
            tab._after_bulk_edit()
            try:
                os.remove(path)
            except OSError:
                pass

    def _autosave_tick(self):
        try:
            os.makedirs(AUTOSAVE_DIR, exist_ok=True)
            for tab in self.tabs:
                path = _autosave_path(tab.index)
                if tab.dirty:
                    try:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(tab.editor.get("1.0", "end-1c"))
                    except Exception:
                        pass
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        finally:
            self.root.after(30000, self._autosave_tick)

    def _clear_autosave(self):
        for path in _list_autosave_files():
            try:
                os.remove(path)
            except OSError:
                pass


def main():
    root = tk.Tk(className="notepaDB")
    root.withdraw()
    root.option_add("*tearOff", False)
    cfg = load_config()
    app = NotepaDBApp(root)
    px = app.theme.px

    geometry = cfg.get("window_geometry")
    zoomed = cfg.get("window_zoomed", True)
    if geometry:
        try:
            root.geometry(geometry)
        except tk.TclError:
            geometry = None
    if not geometry:
        w, h = min(px(1360), root.winfo_screenwidth() - px(80)), min(px(860), root.winfo_screenheight() - px(120))
        root.geometry(f"{w}x{h}+{max(0, (root.winfo_screenwidth() - w) // 2)}+{max(0, (root.winfo_screenheight() - h) // 2)}")
    root.deiconify()
    if zoomed:
        try:
            root.state("zoomed")
        except tk.TclError:
            try:
                root.attributes("-zoomed", True)
            except tk.TclError:
                pass
    root.after(150, app.startup)
    root.mainloop()


if __name__ == "__main__":
    main()
