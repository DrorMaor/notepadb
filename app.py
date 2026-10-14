"""
notepadb — a notepad/scratchpad, but only for databases.

Type SQL, get autocomplete for real table/column names from the server you're connected to, run it, see results. 
SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, and SQLite are all supported via db_backends.py.

Tabs are independent sessions (like SSMS query windows): each tab has its own connection, schema browser, and results grid.

Run with:  py app.py
"""

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
from tkinter import ttk, messagebox, filedialog, simpledialog
from tkinter import font as tkfont

from tksheet import Sheet

from db_backends import BACKENDS, BackendError
from autocomplete import get_suggestions, SQL_KEYWORDS

# When frozen by PyInstaller, __file__ points into a temp extraction dir that
# changes every launch (onefile mode), so config saved there would never
# persist. Use the .exe's own directory instead in that case.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, ".notepadb_config.json")
HISTORY_PATH = os.path.join(APP_DIR, ".notepadb_history.json")
HISTORY_LIMIT = 300
SESSION_PATH = os.path.join(APP_DIR, ".notepadb_session.json")
AUTOSAVE_DIR = os.path.join(APP_DIR, ".notepadb_autosave")

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


def split_statements(sql):
    """Split a batch on ';' while skipping over string literals and
    comments, so a semicolon inside a quoted string or a comment doesn't
    split a statement in half."""
    statements = []
    buf = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            m = STRING_PATTERN.match(sql, i)
            if m:
                buf.append(sql[i:m.end()])
                i = m.end()
                continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i)
            end = n if end == -1 else end + 2
            buf.append(sql[i:end])
            i = end
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _sql_snippet(sql, limit=80):
    snippet = " ".join(sql.split())
    return snippet if len(snippet) <= limit else snippet[:limit - 1] + "…"


def _quote_ident(name):
    return f'"{name}"'


ENGINE_DISPLAY_NAMES = {
    "sqlserver": "SQL Server",
    "postgres": "PostgreSQL",
    "mysql": "MySQL / MariaDB",
    "oracle": "Oracle",
    "sqlite": "SQLite",
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


class Autocomplete:
    """Floating suggestion popup for the SQL editor. `owner` is a QueryTab —
    this only needs owner.schema / owner.editor / owner.highlight_syntax()."""

    def __init__(self, owner):
        self.app = owner
        self.popup = None
        self.listbox = None
        self.prefix_start = None

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
        self.popup.attributes("-topmost", True)
        self.listbox = tk.Listbox(
            self.popup, activestyle="dotbox", font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", selectbackground="#094771",
            selectforeground="#ffffff", highlightthickness=1,
            highlightbackground="#3c3c3c", borderwidth=0,
        )
        self.listbox.pack()
        self.listbox.bind("<Double-Button-1>", self.accept)
        self.listbox.bind("<ButtonRelease-1>", lambda e: self.app.editor.focus_set())

    def _show(self, suggestions):
        self._ensure_popup()
        self.listbox.delete(0, tk.END)
        for s in suggestions:
            self.listbox.insert(tk.END, s)
        self.listbox.configure(height=min(8, len(suggestions)))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(0)

        text_widget = self.app.editor
        bbox = text_widget.bbox(tk.INSERT)
        if bbox:
            x, y, _w, h = bbox
            abs_x = text_widget.winfo_rootx() + x
            abs_y = text_widget.winfo_rooty() + y + h + 2
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
        if not sel:
            self.hide()
            return None
        value = self.listbox.get(sel[0])
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


class ConnectDialog(tk.Toplevel):
    def __init__(self, parent, defaults):
        super().__init__(parent)
        self.title("Connect to database")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self._build(defaults)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self, defaults):
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        row = 0
        ttk.Label(frame, text="Engine:").grid(row=row, column=0, sticky="e", **pad)
        self.engine_var = tk.StringVar(value=defaults.get("engine", "sqlserver"))
        self._engine_display_var = tk.StringVar()
        engine_box = ttk.Combobox(
            frame, textvariable=self._engine_display_var, state="readonly", width=28,
        )
        engine_box.grid(row=row, column=1, sticky="w", **pad)

        # Show friendly labels but keep engine keys underneath.
        self._engine_display_map = {v: k for k, v in ENGINE_DISPLAY_NAMES.items()}
        engine_box["values"] = list(self._engine_display_map.keys())
        self._engine_display_var.set(ENGINE_DISPLAY_NAMES[self.engine_var.get()])

        def on_engine_pick(event=None):
            display = self._engine_display_var.get()
            self.engine_var.set(self._engine_display_map[display])
            self._sync_engine_fields()

        engine_box.bind("<<ComboboxSelected>>", on_engine_pick)
        row += 1

        ttk.Label(frame, text="Saved Connection:").grid(row=row, column=0, sticky="e", **pad)
        self.saved_var = tk.StringVar()
        saved_row = ttk.Frame(frame)
        saved_row.grid(row=row, column=1, sticky="w", **pad)
        self._saved_profiles = defaults.get("saved_connections", {})
        self.saved_box = ttk.Combobox(
            saved_row, textvariable=self.saved_var, state="readonly", width=18,
            values=sorted(self._saved_profiles.keys()),
        )
        self.saved_box.pack(side="left")
        self.saved_box.bind("<<ComboboxSelected>>", self._on_saved_pick)
        ttk.Button(saved_row, text="Save As...", width=9, command=self._save_connection_profile).pack(side="left", padx=(4, 0))
        ttk.Button(saved_row, text="Delete", width=7, command=self._delete_connection_profile).pack(side="left", padx=(4, 0))
        row += 1

        self.server_label = ttk.Label(frame, text="Server:")
        self.server_label.grid(row=row, column=0, sticky="e", **pad)
        self.server_var = tk.StringVar(value=defaults.get("server", "localhost"))
        server_row = ttk.Frame(frame)
        server_row.grid(row=row, column=1, sticky="w", **pad)
        self.server_box = ttk.Combobox(
            server_row, textvariable=self.server_var, width=28,
            values=defaults.get("recent_servers", []),
        )
        self.server_box.pack(side="left")
        self.browse_btn = ttk.Button(server_row, text="Browse...", width=9, command=self._browse_file)
        row += 1

        self.port_label = ttk.Label(frame, text="Port (optional):")
        self.port_label.grid(row=row, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar(value=defaults.get("port", ""))
        self.port_entry = ttk.Entry(frame, textvariable=self.port_var, width=30)
        self.port_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.database_label = ttk.Label(frame, text="Database (optional):")
        self.database_label.grid(row=row, column=0, sticky="e", **pad)
        self.database_var = tk.StringVar(value=defaults.get("database", ""))
        self.database_box = ttk.Combobox(
            frame, textvariable=self.database_var, width=28,
            values=defaults.get("recent_databases", []),
        )
        self.database_box.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.auth_label = ttk.Label(frame, text="Authentication:")
        self.auth_label.grid(row=row, column=0, sticky="e", **pad)
        self.auth_var = tk.StringVar(value=defaults.get("auth_mode", "windows"))
        self.auth_frame = ttk.Frame(frame)
        self.auth_frame.grid(row=row, column=1, sticky="w", **pad)
        ttk.Radiobutton(self.auth_frame, text="Windows", variable=self.auth_var, value="windows",
                        command=self._sync_auth_fields).pack(side="left")
        ttk.Radiobutton(self.auth_frame, text="SQL Login", variable=self.auth_var, value="sql",
                        command=self._sync_auth_fields).pack(side="left")
        row += 1

        self.username_label = ttk.Label(frame, text="Username:")
        self.username_label.grid(row=row, column=0, sticky="e", **pad)
        self.username_var = tk.StringVar(value=defaults.get("username", ""))
        self.username_entry = ttk.Entry(frame, textvariable=self.username_var, width=30)
        self.username_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.password_label = ttk.Label(frame, text="Password:")
        self.password_label.grid(row=row, column=0, sticky="e", **pad)
        self.password_var = tk.StringVar(value="")
        self.password_entry = ttk.Entry(frame, textvariable=self.password_var, width=30, show="*")
        self.password_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.opts_frame = ttk.Frame(frame)
        self.opts_frame.grid(row=row, column=1, sticky="w", **pad)
        self.encrypt_var = tk.BooleanVar(value=defaults.get("encrypt", True))
        self.trust_var = tk.BooleanVar(value=defaults.get("trust_cert", True))
        ttk.Checkbutton(self.opts_frame, text="Encrypt", variable=self.encrypt_var).pack(side="left")
        ttk.Checkbutton(self.opts_frame, text="Trust server certificate", variable=self.trust_var).pack(side="left")
        row += 1

        self.status_label = ttk.Label(frame, text="", foreground="#b00020")
        self.status_label.grid(row=row, column=0, columnspan=2, sticky="w", padx=8)
        row += 1

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=(10, 0))
        self.connect_btn = ttk.Button(btn_frame, text="Connect", command=self._connect, default="active")
        self.connect_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side="left", padx=4)

        self._sync_auth_fields()
        self._sync_engine_fields()
        self.bind("<Return>", lambda e: self._connect())
        # focus_set() before the window is actually mapped doesn't stick —
        # defer it until after the Toplevel has appeared on screen.
        self.after_idle(self.connect_btn.focus_set)

    def _sync_auth_fields(self):
        is_sql = self.auth_var.get() == "sql"
        state = "normal" if is_sql else "disabled"
        self.username_entry.configure(state=state)
        self.password_entry.configure(state=state)

    def _sync_engine_fields(self):
        # SQLite is a local file, not a client/server connection — swap the
        # "Server" field for a file picker and hide everything that only
        # makes sense for a networked engine (port, auth, credentials, TLS).
        is_sqlite = self.engine_var.get() == "sqlite"
        self.server_label.configure(text="File path:" if is_sqlite else "Server:")
        if is_sqlite:
            self.browse_btn.pack(side="left", padx=(4, 0))
        else:
            self.browse_btn.pack_forget()

        hideable = [
            self.port_label, self.port_entry, self.database_label, self.database_box,
            self.auth_label, self.auth_frame, self.username_label, self.username_entry,
            self.password_label, self.password_entry, self.opts_frame,
        ]
        for widget in hideable:
            if is_sqlite:
                widget.grid_remove()
            else:
                widget.grid()
        if not is_sqlite:
            self._sync_auth_fields()

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select SQLite database file",
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3 *.db3"), ("All files", "*.*")],
        )
        if path:
            self.server_var.set(path)

    def _on_saved_pick(self, event=None):
        profile = self._saved_profiles.get(self.saved_var.get())
        if not profile:
            return
        self.engine_var.set(profile.get("engine", "sqlserver"))
        self._engine_display_var.set(ENGINE_DISPLAY_NAMES[self.engine_var.get()])
        self.server_var.set(profile.get("server", ""))
        self.port_var.set(profile.get("port") or "")
        self.database_var.set(profile.get("database") or "")
        self.auth_var.set(profile.get("auth_mode", "windows"))
        self.username_var.set(profile.get("username", ""))
        self.encrypt_var.set(profile.get("encrypt", True))
        self.trust_var.set(profile.get("trust_cert", True))
        self._sync_engine_fields()

    def _save_connection_profile(self):
        name = simpledialog.askstring("Save Connection", "Name for this connection:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        cfg = load_config()
        profiles = cfg.setdefault("saved_connections", {})
        profiles[name] = {
            "engine": self.engine_var.get(),
            "server": self.server_var.get().strip(),
            "port": self.port_var.get().strip() or None,
            "database": self.database_var.get().strip() or None,
            "auth_mode": self.auth_var.get(),
            "username": self.username_var.get().strip(),
            "encrypt": self.encrypt_var.get(),
            "trust_cert": self.trust_var.get(),
        }
        save_config(cfg)
        self._saved_profiles = profiles
        self.saved_box["values"] = sorted(profiles.keys())
        self.saved_var.set(name)
        self.status_label.configure(text=f"Saved '{name}'.", foreground="#0a7a0a")

    def _delete_connection_profile(self):
        name = self.saved_var.get()
        if not name or name not in self._saved_profiles:
            return
        if not messagebox.askyesno("Delete connection", f"Delete saved connection '{name}'?"):
            return
        cfg = load_config()
        profiles = cfg.get("saved_connections", {})
        profiles.pop(name, None)
        save_config(cfg)
        self._saved_profiles = profiles
        self.saved_box["values"] = sorted(profiles.keys())
        self.saved_var.set("")
        self.status_label.configure(text=f"Deleted '{name}'.", foreground="#b00020")

    def _cancel(self):
        self.result = None
        self.destroy()

    def _connect(self):
        engine = self.engine_var.get()
        if engine == "sqlite":
            file_path = self.server_var.get().strip()
            if not file_path:
                self.status_label.configure(text="File path is required.")
                return
            self.result = {
                "engine": engine,
                "server": file_path,
                "port": None,
                "database": os.path.basename(file_path),
                "auth_mode": "windows",
                "username": "",
                "password": "",
                "encrypt": True,
                "trust_cert": True,
            }
            self.destroy()
            return
        if not self.server_var.get().strip():
            self.status_label.configure(text="Server is required.")
            return
        self.result = {
            "engine": engine,
            "server": self.server_var.get().strip(),
            "port": self.port_var.get().strip() or None,
            "database": self.database_var.get().strip() or None,
            "auth_mode": self.auth_var.get(),
            "username": self.username_var.get().strip(),
            "password": self.password_var.get(),
            "encrypt": self.encrypt_var.get(),
            "trust_cert": self.trust_var.get(),
        }
        self.destroy()


class FindDialog(tk.Toplevel):
    """Non-modal Find/Replace dialog, one per tab. Stays open across
    searches; F3/Shift+F3 keep working off the tab's own find_var even
    while this is closed."""

    def __init__(self, owner):
        super().__init__(owner.root)
        self.app = owner
        self.title("Find / Replace")
        self.resizable(False, False)
        self.transient(owner.root)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(frame, text="Find:").grid(row=0, column=0, sticky="e", **pad)
        self.find_entry = ttk.Entry(frame, textvariable=self.app.find_var, width=30)
        self.find_entry.grid(row=0, column=1, columnspan=3, sticky="we", **pad)
        self.find_entry.bind("<Return>", lambda e: self.app.find_next())

        ttk.Label(frame, text="Replace with:").grid(row=1, column=0, sticky="e", **pad)
        self.replace_entry = ttk.Entry(frame, textvariable=self.app.replace_var, width=30)
        self.replace_entry.grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        self.replace_entry.bind("<Return>", lambda e: self.app.replace_current())

        ttk.Checkbutton(
            frame, text="Match case", variable=self.app.match_case_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=4, pady=(8, 0))
        ttk.Button(btn_frame, text="Find Next", command=self.app.find_next).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Find Previous", command=self.app.find_previous).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Replace", command=self.app.replace_current).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Replace All", command=self.app.replace_all).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=self.withdraw).pack(side="left", padx=4)

    def focus_find(self):
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")

    def focus_replace(self):
        self.replace_entry.focus_set()
        self.replace_entry.select_range(0, "end")


class HistoryDialog(tk.Toplevel):
    """Non-modal browser over every successfully-run statement, newest
    first. Double-click (or Insert) drops it into the active tab's editor;
    Insert & Run also executes it immediately."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Query History")
        self.geometry("760x420")
        self.transient(app.root)
        self.history = load_history()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self):
        columns = ("time", "elapsed", "rows", "connection", "sql")
        self.tree = ttk.Treeview(self, columns=columns, show="headings")
        headers = {
            "time": ("Time", 140), "elapsed": ("ms", 70), "rows": ("Rows", 70),
            "connection": ("Connection", 160), "sql": ("SQL", 320),
        }
        for key, (text, width) in headers.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor="w" if key in ("time", "connection", "sql") else "e")
        self.tree.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        self._populate()
        self.tree.bind("<Double-Button-1>", lambda e: self._insert_selected())

        btn_frame = ttk.Frame(self)
        btn_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="Insert", command=self._insert_selected).pack(side="left")
        ttk.Button(btn_frame, text="Insert && Run", command=lambda: self._insert_selected(run=True)).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Clear History", command=self._clear).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(side="right")

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        for entry in self.history:
            self.tree.insert("", "end", values=(
                entry.get("time", ""), entry.get("elapsed_ms", ""), entry.get("rows", ""),
                entry.get("connection", ""), _sql_snippet(entry.get("sql", ""), limit=120),
            ))

    def _selected_entry(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = self.tree.index(sel[0])
        return self.history[idx] if idx < len(self.history) else None

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
        if not messagebox.askyesno("Clear history", "Delete all saved query history?"):
            return
        save_history([])
        self.history = []
        self._populate()


class QueryTab:
    """One independent session: own connection, schema browser, editor and
    results grid. Conceptually the same as one SSMS query window."""

    TITLE_PREFIX = "Query"

    def __init__(self, app, index):
        self.app = app
        self.index = index
        self.title = f"{self.TITLE_PREFIX} {index}"
        self.current_file = None
        self.dirty = False

        self.backend = None
        self.connected_server = None
        self._last_auth_mode = "windows"
        self.databases = []  # all database names on the connected server
        self.db_schema_cache = {}  # db_name -> {'schema.table': [(col, type), ...]}
        self.active_database = None  # db the connection is USE'd into, for Run/autocomplete
        self.schema = None  # active db's schema

        self.find_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        self.match_case_var = tk.BooleanVar(value=False)
        self.find_dialog = None

        self._work_queue = queue.Queue()
        self._closed = False
        self._query_running = False
        self._cancel_requested = False

        self.frame = ttk.Frame(app.notebook)
        self._build_body()
        self.apply_theme(self.app.dark_mode_var.get())
        self.apply_word_wrap(self.app.word_wrap_var.get())
        self.apply_line_numbers_visibility(self.app.show_line_numbers_var.get())
        self._log_message("Ready.")

        self.autocomplete = Autocomplete(self)
        self._bind_editor_events()
        self.highlight_syntax()

        self.root.after(100, self._poll_queue)

    @property
    def root(self):
        return self.app.root

    # ---------- body construction ----------

    def _build_body(self):
        main = ttk.PanedWindow(self.frame, orient="horizontal")
        main.pack(side="top", fill="both", expand=True)

        # --- left: schema browser ---
        left = ttk.Frame(main, width=260)
        search_frame = ttk.Frame(left)
        search_frame.pack(side="top", fill="x")
        ttk.Label(search_frame, text="Filter:").pack(side="left", padx=(4, 2))
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(search_frame, textvariable=self.filter_var)
        filter_entry.pack(side="left", fill="x", expand=True, padx=(0, 4), pady=4)
        filter_entry.bind("<KeyRelease>", lambda e: self.populate_schema_tree())

        self.schema_tree = ttk.Treeview(left, show="tree")
        self.schema_tree.pack(side="top", fill="both", expand=True)
        self.schema_tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.schema_tree.bind("<Button-3>", self._on_tree_right_click)
        self.schema_tree.bind("<ButtonPress-1>", self._on_tree_press, add="+")
        self.schema_tree.bind("<B1-Motion>", self._on_tree_motion, add="+")
        self.schema_tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")
        self.schema_tree.bind("<KeyPress>", self._on_tree_keypress)
        self.schema_tree.bind("<Motion>", self._on_tree_hover, add="+")
        self.schema_tree.bind("<Leave>", self._on_tree_leave, add="+")
        main.add(left, weight=0)

        self._tree_search_buffer = ""
        self._tree_search_last_time = 0.0
        self._tooltip_window = None
        self._tooltip_after_id = None
        self._tooltip_item = None
        self._drag_data = None
        self._drag_ghost = None

        # --- right: editor + results, stacked vertically, with a message
        # log pinned below (outside the resizable panes). ---
        right_outer = ttk.Frame(main)
        main.add(right_outer, weight=1)

        right = ttk.PanedWindow(right_outer, orient="vertical")
        # Packed after log_frame below — pack() allocates space in call
        # order, so the log panel must claim its fixed-height slice of
        # right_outer first, or this expand=True pane eats the whole area.

        editor_frame = ttk.Frame(right)
        self.line_numbers = tk.Text(
            editor_frame, width=4, padx=4, takefocus=0, border=0,
            background="#f0f0f0", state="disabled", font=("Consolas", 11),
        )
        self.line_numbers.pack(side="left", fill="y")

        self.editor = tk.Text(
            editor_frame, wrap="none", undo=True, font=("Consolas", 11),
            insertbackground="black",
        )
        self.editor.pack(side="left", fill="both", expand=True)
        editor_scroll = ttk.Scrollbar(editor_frame, orient="vertical", command=self._on_editor_scroll)
        editor_scroll.pack(side="right", fill="y")
        self.editor.configure(yscrollcommand=lambda a, b: self._on_editor_yscroll(a, b, editor_scroll))
        right.add(editor_frame, weight=3)

        results_frame = ttk.Frame(right)
        # A batch can contain several statements, each producing its own
        # result set — one tab per result set, so none of them get clobbered
        # by the next.
        self.results_notebook = ttk.Notebook(results_frame)
        self.results_notebook.grid(row=0, column=0, sticky="nsew")
        results_frame.rowconfigure(0, weight=1)
        results_frame.columnconfigure(0, weight=1)
        self._result_sheets = []
        self._add_placeholder_result_tab()
        right.add(results_frame, weight=2)

        log_frame = ttk.Frame(right_outer)
        log_frame.pack(side="bottom", fill="x")
        ttk.Label(log_frame, text="Messages:", anchor="w").pack(side="top", fill="x", padx=6, pady=(4, 0))
        log_body = ttk.Frame(log_frame)
        log_body.pack(side="top", fill="x")
        self.status_log = tk.Text(
            log_body, height=5, wrap="none", state="disabled",
            font=("Consolas", 9), relief="flat", padx=6, pady=2,
        )
        self.status_log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.status_log.yview)
        log_scroll.pack(side="right", fill="y")
        self.status_log.configure(yscrollcommand=log_scroll.set)

        right.pack(side="top", fill="both", expand=True)

    def apply_word_wrap(self, enabled):
        self.editor.configure(wrap="word" if enabled else "none")

    def apply_line_numbers_visibility(self, show):
        if show:
            self.line_numbers.pack(side="left", fill="y", before=self.editor)
        else:
            self.line_numbers.pack_forget()

    def apply_theme(self, dark):
        if dark:
            editor_bg, editor_fg, editor_insert = "#1e1e1e", "#d4d4d4", "#ffffff"
            ln_bg, ln_fg = "#252526", "#6e7681"
            kw_c, str_c, comment_c, num_c = "#569cd6", "#ce9178", "#6a9955", "#b5cea8"
            sheet_theme = "dark blue"
            log_bg, log_fg = "#1e1e1e", "#cccccc"
        else:
            editor_bg, editor_fg, editor_insert = "#ffffff", "#000000", "black"
            ln_bg, ln_fg = "#f0f0f0", "#000000"
            kw_c, str_c, comment_c, num_c = "#0000cc", "#a31515", "#008000", "#098658"
            sheet_theme = "light blue"
            log_bg, log_fg = "#f5f5f5", "#333333"

        self.editor.configure(background=editor_bg, foreground=editor_fg, insertbackground=editor_insert)
        self.line_numbers.configure(background=ln_bg, foreground=ln_fg)
        self.status_log.configure(background=log_bg, foreground=log_fg)
        self.editor.tag_configure("kw", foreground=kw_c, font=("Consolas", 11, "bold"))
        self.editor.tag_configure("str", foreground=str_c)
        self.editor.tag_configure("comment", foreground=comment_c)
        self.editor.tag_configure("num", foreground=num_c)
        self.editor.tag_configure("bracket", background="#808080" if dark else "#c0c0c0")
        self.editor.tag_raise("bracket")
        self._sheet_theme = sheet_theme
        for sheet in self._result_sheets:
            try:
                sheet.change_theme(sheet_theme)
            except Exception:
                pass

    def _log_message(self, text):
        line = f"[{time.strftime('%H:%M:%S')}] {text}\n"
        self.status_log.configure(state="normal")
        self.status_log.insert("end", line)
        self.status_log.see("end")
        self.status_log.configure(state="disabled")

    # ---------- editor scroll / line numbers ----------

    def _on_editor_scroll(self, *args):
        self.editor.yview(*args)
        self.line_numbers.yview(*args)

    def _on_editor_yscroll(self, first, last, scrollbar):
        scrollbar.set(first, last)
        self.line_numbers.yview_moveto(first)

    def _update_line_numbers(self):
        total_lines = int(self.editor.index("end-1c").split(".")[0])
        content = "\n".join(str(i) for i in range(1, total_lines + 1))
        self.line_numbers.configure(state="normal")
        self.line_numbers.delete("1.0", "end")
        self.line_numbers.insert("1.0", content)
        self.line_numbers.configure(state="disabled")
        self.line_numbers.yview_moveto(self.editor.yview()[0])

    # ---------- editor events ----------

    def _bind_editor_events(self):
        self.editor.bind("<KeyRelease>", self._on_editor_key_release)
        self.editor.bind("<Up>", lambda e: "break" if self.autocomplete.move(-1) else None)
        self.editor.bind("<Down>", lambda e: "break" if self.autocomplete.move(1) else None)
        self.editor.bind("<Return>", self._on_return)
        self.editor.bind("<Tab>", self._on_tab)
        self.editor.bind("<Escape>", self._on_escape)
        self.editor.bind("<ButtonPress-1>", lambda e: self.autocomplete.hide())
        self.editor.bind("<ButtonRelease-1>", lambda e: self._update_bracket_match(), add="+")
        self.editor.bind("<<Modified>>", self._on_modified_flag)
        self.editor.bind("<Control-slash>", self.toggle_comment)

        # Tk's Text widget has built-in Emacs-style bindings for Control-o
        # (insert newline), Control-t (transpose chars), and Control-k
        # (delete to end of line) that would otherwise fire — and silently
        # mutate the buffer — before our own shortcuts for Open/New Tab/
        # Connect get a chance to run.
        self.editor.bind("<Control-o>", lambda e: (self.app.open_file(), "break")[1])
        self.editor.bind("<Control-t>", lambda e: (self.app.new_tab(), "break")[1])
        self.editor.bind("<Control-k>", lambda e: (self.app._dispatch("open_connect_dialog"), "break")[1])

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
        if event.keysym in ("Up", "Down", "Return", "Tab", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            self._update_bracket_match()
            return
        self.highlight_syntax()
        self._update_line_numbers()
        self.autocomplete.update_suggestions()
        self._update_bracket_match()

    def _on_return(self, event):
        if self.autocomplete.visible():
            return self.autocomplete.accept()
        line_start = self.editor.index("insert linestart")
        line_end = self.editor.index("insert lineend")
        current_line = self.editor.get(line_start, line_end)
        indent = re.match(r"[ \t]*", current_line).group(0)
        self.editor.insert(tk.INSERT, "\n" + indent)
        self.highlight_syntax()
        self.root.after_idle(self._update_line_numbers)
        return "break"

    def _on_tab(self, event):
        if self.autocomplete.visible():
            return self.autocomplete.accept()
        self.editor.insert(tk.INSERT, "    ")
        return "break"

    def _on_escape(self, event):
        if self.autocomplete.visible():
            self.autocomplete.hide()
            return "break"
        return None

    def highlight_syntax(self):
        text = self.editor.get("1.0", "end-1c")
        for tag in ("kw", "str", "comment", "num"):
            self.editor.tag_remove(tag, "1.0", "end")

        def mark(pattern, tag):
            for m in pattern.finditer(text):
                start, end = m.span()
                self.editor.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")

        mark(KEYWORD_PATTERN, "kw")
        mark(NUMBER_PATTERN, "num")
        mark(STRING_PATTERN, "str")
        mark(LINE_COMMENT_PATTERN, "comment")
        mark(BLOCK_COMMENT_PATTERN, "comment")

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
        return "break"

    # ---------- schema tree ----------

    def populate_schema_tree(self):
        self._cancel_tooltip_timer()
        self._hide_tooltip()
        self._tooltip_item = None
        self.schema_tree.delete(*self.schema_tree.get_children())
        if not self.databases:
            return
        filt = self.filter_var.get().strip().lower()
        for db_name in sorted(self.databases, key=str.lower):
            db_node = self.schema_tree.insert(
                "", "end", text=db_name, tags=("database",),
                open=(db_name == self.active_database),
            )
            if db_name == self.active_database:
                self.schema_tree.item(db_node, text=f"{db_name}  (active)")
            cached = self.db_schema_cache.get(db_name)
            if cached is not None:
                self._populate_tables(db_node, cached, filt)
            else:
                self.schema_tree.insert(db_node, "end", text="(expand to load tables)", tags=("placeholder",))

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
        db_name = self.schema_tree.item(item, "text").split("  (active)")[0]
        if db_name in self.db_schema_cache:
            return
        self._load_database_schema(item, db_name)

    def _load_database_schema(self, db_node, db_name):
        self.app.set_status(self, f"Loading tables for {db_name}...")

        def work():
            return self.backend.list_schema(db_name)

        def done(result, error):
            if error is not None:
                self.app.set_status(self, f"Failed to load {db_name}.")
                messagebox.showerror("Load failed", str(error))
                return
            self.db_schema_cache[db_name] = result
            if not self.schema_tree.exists(db_node):
                return
            self.schema_tree.delete(*self.schema_tree.get_children(db_node))
            filt = self.filter_var.get().strip().lower()
            self._populate_tables(db_node, result, filt)
            self.app.set_status(self, f"Loaded {len(result)} table(s) from {db_name}.")

        self._run_async(work, done)

    def _set_active_database(self, db_name):
        if self.backend is None or not self.backend.is_connected:
            return
        if db_name == self.active_database:
            return
        self.app.set_status(self, f"Switching to {db_name}...")

        def work():
            self.backend.use_database(db_name)
            return self.db_schema_cache.get(db_name) or self.backend.list_schema(db_name)

        def done(result, error):
            if error is not None:
                self.app.set_status(self, "Switch failed.")
                messagebox.showerror("Switch failed", str(error))
                return
            self.active_database = db_name
            self.db_schema_cache[db_name] = result
            self.schema = result
            self._auto_title(db_name)
            self.app._sync_chrome_if_active(self)
            self.app.set_status(self, f"Using {db_name}. {len(result)} table(s).")
            self.populate_schema_tree()
            existing = load_config()
            existing["server"] = self.connected_server
            existing["database"] = db_name
            existing["recent_databases"] = _with_recent(existing.get("recent_databases", []), db_name)
            save_config(existing)

        self._run_async(work, done)

    def connection_label(self):
        if self.backend is None or not self.backend.is_connected:
            return "Not connected", "#b00020"
        if self.active_database:
            return (
                f"Connected: {self.connected_server}/{self.active_database} ({self.backend.label})",
                "#0a7a0a",
            )
        return f"Connected: {self.connected_server} ({self.backend.label})", "#0a7a0a"

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
            menu.add_command(label=f"Use '{db_name}'", command=lambda: self._set_active_database(db_name))
        elif "table" in tags:
            table_key = self.schema_tree.item(item, "values")[0]
            menu.add_command(label="Select Top 1000 Rows", command=lambda: self._select_top_rows(table_key))
            menu.add_command(label="Script as CREATE TABLE", command=lambda: self._script_table_ddl(table_key))
            menu.add_command(label="Copy Column List", command=lambda: self._copy_column_list(table_key))
            menu.add_separator()
            menu.add_command(label="Export Table to CSV...", command=lambda: self._export_table_csv(table_key))
        else:
            return
        menu.tk_popup(event.x_root, event.y_root)

    # ---------- table context menu actions ----------

    def _cols_for_table(self, table_key):
        if self.schema and table_key in self.schema:
            return self.schema[table_key]
        for db_schema in self.db_schema_cache.values():
            if table_key in db_schema:
                return db_schema[table_key]
        return None

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
        self.highlight_syntax()
        self._update_line_numbers()

    def _insert_and_run(self, sql):
        self._insert_text(sql)
        self.run_query()

    def _select_top_rows(self, table_key):
        if self.backend is None or not self.backend.is_connected:
            return
        self._insert_and_run(self.backend.select_top_sql(table_key))

    def _script_table_ddl(self, table_key):
        cols = self._cols_for_table(table_key)
        if cols is None:
            messagebox.showinfo("Not loaded", "Expand the table first so its columns are loaded.")
            return
        lines = ",\n".join(f"    {_quote_ident(name)} {type_str}" for name, type_str in cols)
        ddl = f"CREATE TABLE {_quote_table_key(table_key)} (\n{lines}\n);"
        tab = self.app.new_tab()
        tab.editor.insert("1.0", ddl)
        tab.set_title(f"{table_key} DDL")
        tab._mark_clean()
        tab.highlight_syntax()
        tab._update_line_numbers()

    def _copy_column_list(self, table_key):
        cols = self._cols_for_table(table_key)
        if cols is None:
            messagebox.showinfo("Not loaded", "Expand the table first so its columns are loaded.")
            return
        text = ", ".join(name for name, _ in cols)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.app.set_status(self, f"Copied {len(cols)} column name(s) to clipboard.")

    def _export_table_csv(self, table_key):
        if self.backend is None or not self.backend.is_connected:
            return
        self.app.set_status(self, f"Exporting {table_key}...")

        def work():
            sql = self.backend.select_top_sql(table_key, limit=100000)
            return self.backend.execute(sql)

        def done(result, error):
            if error is not None:
                self.app.set_status(self, "Export failed.")
                messagebox.showerror("Export failed", str(error))
                return
            columns, rows = result
            path = filedialog.asksaveasfilename(
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                initialfile=f"{table_key.rsplit('.', 1)[-1]}.csv",
            )
            if not path:
                self.app.set_status(self, "Export cancelled.")
                return
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(columns)
                    writer.writerows(rows)
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
                return
            self.app.set_status(self, f"Exported {len(rows)} row(s) to {path}")

        self._run_async(work, done)

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
            500, lambda: self._show_tooltip(item, event.x_root, event.y_root)
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
        self._hide_tooltip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.geometry(f"+{x_root + 12}+{y_root + 8}")
        tk.Label(
            tip, text=text, background="#ffffe0", foreground="black",
            relief="solid", borderwidth=1, font=("Segoe UI", 9), padx=4, pady=2,
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
            "Up", "Down", "Left", "Right", "Return", "Tab", "Escape",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        ):
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

    DRAG_THRESHOLD_PX = 4

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
        self._drag_ghost = tk.Toplevel(self.root)
        self._drag_ghost.wm_overrideredirect(True)
        self._drag_ghost.attributes("-topmost", True)
        try:
            self._drag_ghost.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        self._drag_ghost_label = tk.Label(
            self._drag_ghost, text=text, font=("Consolas", 10),
            bg="#ffffcc", fg="#000000", relief="solid", borderwidth=1, padx=6, pady=2,
        )
        self._drag_ghost_label.pack()

    def _move_drag_ghost(self, x_root, y_root):
        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{x_root + 14}+{y_root + 12}")

    def _update_drag_target_feedback(self, x_root, y_root):
        if self._drag_ghost is None:
            return
        over_editor = self.root.winfo_containing(x_root, y_root) is self.editor
        self._drag_ghost_label.configure(
            bg="#ccffcc" if over_editor else "#ffffcc"
        )

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
        self.highlight_syntax()
        self._update_line_numbers()
        self.editor.focus_set()

    # ---------- file operations ----------

    def load_file(self, path):
        content = read_text_file(path)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", content)
        self.current_file = path
        self.set_title(os.path.basename(path))
        self._mark_clean()
        self.highlight_syntax()
        self._update_line_numbers()
        self.app.set_status(self, f"Opened {path}")

    def save_file(self):
        if not self.current_file:
            return self.save_file_as()
        with open(self.current_file, "w", encoding="utf-8") as f:
            f.write(self.editor.get("1.0", "end-1c"))
        self._mark_clean()
        self.app.set_status(self, f"Saved {self.current_file}")

    def save_file_as(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".sql", filetypes=[("SQL files", "*.sql")], initialfile=self.title,
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
        label = self.title + ("*" if self.dirty else "")
        self.app.notebook.tab(self.frame, text=label)

    def _auto_title(self, base_name):
        # Mirrors SSMS's "SQLQuery1, SQLQuery2, ..." but keyed off the actual
        # database name instead of a generic prefix. Only kicks in before the
        # user has explicitly saved the tab to a file of their own choosing.
        if self.current_file is not None:
            return
        taken = {t.title for t in self.app.tabs if t is not self}
        n = 1
        while f"{base_name}{n}" in taken:
            n += 1
        self.set_title(f"{base_name}{n}")

    # ---------- connection ----------

    def open_connect_dialog(self):
        defaults = load_config()
        dialog = ConnectDialog(self.root, defaults)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self._connect_with(dialog.result)

    def _connect_with(self, params):
        self.app.set_status(self, f"Connecting to {params['server']}...")
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
                self.app.set_status(self, "Connection failed.")
                messagebox.showerror("Connection failed", str(error))
                return
            backend, databases, active_schema, params = result
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
                self.app.set_status(self, f"Connected. {len(active_schema)} tables loaded.")
            else:
                self.app.set_status(self, f"Connected. {len(databases)} database(s) found — pick one to drill in.")
            to_save = dict(params)
            to_save.pop("password", None)
            existing = load_config()
            to_save["recent_servers"] = _with_recent(
                existing.get("recent_servers", []), params["server"]
            )
            to_save["recent_databases"] = _with_recent(
                existing.get("recent_databases", []), params["database"] or ""
            )
            save_config(to_save)

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
        self.app.set_status(self, "Disconnected.")

    def refresh_schema(self):
        if self.backend is None or not self.backend.is_connected:
            messagebox.showinfo("Not connected", "Connect to a database first.")
            return
        self.app.set_status(self, "Refreshing schema...")

        def work():
            databases = self.backend.list_databases()
            active_schema = self.backend.list_schema(self.active_database) if self.active_database else None
            return databases, active_schema

        def done(result, error):
            if error is not None:
                self.app.set_status(self, "Schema refresh failed.")
                messagebox.showerror("Schema refresh failed", str(error))
                return
            databases, active_schema = result
            self.databases = databases
            self.db_schema_cache = {}
            if self.active_database:
                self.db_schema_cache[self.active_database] = active_schema
            self.schema = active_schema
            self.populate_schema_tree()
            self.app.set_status(self, f"Schema refreshed. {len(databases)} database(s).")

        self._run_async(work, done)

    # ---------- find / replace ----------

    def _ensure_find_dialog(self):
        if self.find_dialog is None or not self.find_dialog.winfo_exists():
            self.find_dialog = FindDialog(self)
        self.find_dialog.deiconify()
        self.find_dialog.lift()
        return self.find_dialog

    def open_find_dialog(self):
        self._ensure_find_dialog().focus_find()

    def open_replace_dialog(self):
        self._ensure_find_dialog().focus_replace()

    def _find(self, query, start, backwards):
        if backwards:
            return self.editor.search(
                query, start, stopindex="1.0", backwards=True, nocase=not self.match_case_var.get(),
            )
        return self.editor.search(
            query, start, stopindex="end", nocase=not self.match_case_var.get(),
        )

    def _select_match(self, idx, query, place_cursor_at_start):
        end = f"{idx}+{len(query)}c"
        self.editor.tag_remove("sel", "1.0", "end")
        self.editor.tag_add("sel", idx, end)
        self.editor.mark_set(tk.INSERT, idx if place_cursor_at_start else end)
        self.editor.see(idx)
        self.editor.focus_set()

    def find_next(self):
        query = self.find_var.get()
        if not query:
            self.app.set_status(self, "Nothing to find.")
            return
        try:
            start = self.editor.index("sel.last")
        except tk.TclError:
            start = self.editor.index(tk.INSERT)
        idx = self._find(query, start, backwards=False) or self._find(query, "1.0", backwards=False)
        if not idx:
            self.app.set_status(self, f'"{query}" not found.')
            return
        self._select_match(idx, query, place_cursor_at_start=False)
        self.app.set_status(self, f'Found "{query}".')

    def find_previous(self):
        query = self.find_var.get()
        if not query:
            self.app.set_status(self, "Nothing to find.")
            return
        try:
            start = self.editor.index("sel.first")
        except tk.TclError:
            start = self.editor.index(tk.INSERT)
        idx = self._find(query, start, backwards=True) or self._find(query, "end", backwards=True)
        if not idx:
            self.app.set_status(self, f'"{query}" not found.')
            return
        self._select_match(idx, query, place_cursor_at_start=True)
        self.app.set_status(self, f'Found "{query}".')

    def replace_current(self):
        query = self.find_var.get()
        if not query:
            self.app.set_status(self, "Nothing to find.")
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
            self.app.set_status(self, "Nothing to find.")
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
        self.app.set_status(self, f"Replaced {count} occurrence(s).")

    # ---------- formatting ----------

    def format_sql(self):
        try:
            import sqlparse
        except ImportError:
            messagebox.showerror(
                "Missing dependency",
                "SQL formatting requires the 'sqlparse' package.\nInstall it with: pip install sqlparse",
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
            self.app.set_status(self, "Nothing to format.")
            return
        formatted = sqlparse.format(text, reindent=True, keyword_case="upper").strip("\n")
        self.editor.delete(sel_start, sel_end)
        self.editor.insert(sel_start, formatted)
        self.highlight_syntax()
        self._update_line_numbers()
        self.app.set_status(self, "Formatted selection." if has_selection else "Formatted query.")

    # ---------- query execution ----------

    def run_query(self):
        if self.backend is None or not self.backend.is_connected:
            messagebox.showinfo("Not connected", "Connect to a database first.")
            return
        try:
            sel_start, sel_end = self.editor.index("sel.first"), self.editor.index("sel.last")
            sql_text = self.editor.get(sel_start, sel_end)
        except tk.TclError:
            sql_text = self.editor.get("1.0", "end-1c")
        self._execute_sql_text(sql_text)

    def explain_query(self):
        if self.backend is None or not self.backend.is_connected:
            messagebox.showinfo("Not connected", "Connect to a database first.")
            return
        try:
            sel_start, sel_end = self.editor.index("sel.first"), self.editor.index("sel.last")
            sql_text = self.editor.get(sel_start, sel_end)
        except tk.TclError:
            sql_text = self.editor.get("1.0", "end-1c")
        statements = split_statements(sql_text)
        if not statements:
            self.app.set_status(self, "Nothing to explain.")
            return
        if len(statements) > 1:
            messagebox.showinfo("Explain Plan", "Select (or write) just one statement to explain.")
            return
        self._execute_sql_text(self.backend.explain_statement(statements[0]), is_explain=True)

    def _execute_sql_text(self, sql_text, is_explain=False):
        if self._query_running:
            self.app.set_status(self, "A query is already running.")
            return
        statements = split_statements(sql_text)
        if not statements:
            self.app.set_status(self, "Nothing to run.")
            return

        self.app.set_status(self, "Running query...")
        started = time.monotonic()
        self._query_running = True
        self._cancel_requested = False
        self.app._sync_chrome_if_active(self)

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
            self._query_running = False
            self.app._sync_chrome_if_active(self)
            elapsed_ms = (time.monotonic() - started) * 1000
            if error is not None:
                if self._cancel_requested:
                    self.app.set_status(self, f"Query cancelled after {elapsed_ms:.0f} ms.")
                else:
                    self.app.set_status(self, f"Error after {elapsed_ms:.0f} ms.")
                    messagebox.showerror("Query error", str(error))
                return
            self._show_batch_results(results, elapsed_ms, record=not is_explain)

        self._run_async(work, done)

    def cancel_query(self):
        if not self._query_running or self.backend is None:
            return
        self._cancel_requested = True
        self.app.set_status(self, "Sending cancel request...")

        backend = self.backend

        def work():
            return backend.cancel()

        def done(result, error):
            if error is not None:
                self.app.set_status(self, f"Cancel request errored: {error}")
            elif not result:
                self.app.set_status(self, "Cancel request failed — query may still be running.")
            elif self._query_running:
                self.app.set_status(self, "Cancel request sent — waiting for the server to stop the query...")

        self._run_async(work, done)

    def _history_connection_label(self):
        if self.connected_server is None:
            return "?"
        parts = [self.connected_server]
        if self.active_database:
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
                    self._log_message(f"Cancelled: {_sql_snippet(stmt)}")
                else:
                    self._log_message(f"Error: {err} — {_sql_snippet(stmt)}")
                continue
            if columns is None:
                affected = rows
                self._log_message(f"OK. {affected} row(s) affected. — {_sql_snippet(stmt)}")
                if record:
                    record_history(stmt, elapsed_ms, affected, conn_label)
                continue
            result_count += 1
            total_rows += len(rows)
            self._add_result_tab(result_count, columns, rows, select=(result_count == 1))
            if record:
                record_history(stmt, elapsed_ms, len(rows), conn_label)

        if result_count == 0 and not had_error:
            self._add_placeholder_result_tab()

        # A lone failing statement keeps the old single-query UX (a modal);
        # a multi-statement batch just logs each outcome instead of a
        # popup per statement.
        if had_error and len(results) == 1 and not self._cancel_requested:
            messagebox.showerror("Query error", str(results[0][3]))

        if had_error and self._cancel_requested:
            self.app.set_status(self, f"Query cancelled after {elapsed_ms:.0f} ms.")
        elif had_error:
            self.app.set_status(self, f"Completed with error(s) after {elapsed_ms:.0f} ms.")
        elif result_count:
            self.app.set_status(self, f"{total_rows} row(s) total across {result_count} result set(s) ({elapsed_ms:.0f} ms).")
        else:
            self.app.set_status(self, f"OK ({elapsed_ms:.0f} ms).")

    def _clear_result_tabs(self):
        for tab_id in self.results_notebook.tabs():
            self.results_notebook.forget(tab_id)
        self._result_sheets = []

    def _add_placeholder_result_tab(self):
        frame = ttk.Frame(self.results_notebook)
        ttk.Label(frame, text="(no results yet)", anchor="center").pack(expand=True, fill="both")
        self.results_notebook.add(frame, text="Results")

    def _add_result_tab(self, n, columns, rows, select=True):
        frame = ttk.Frame(self.results_notebook)
        theme = getattr(self, "_sheet_theme", "dark blue" if self.app.dark_mode_var.get() else "light blue")
        sheet = Sheet(
            frame, headers=[], data=[],
            font=("Consolas", 10, "normal"), header_font=("Consolas", 10, "bold"),
            theme=theme,
        )
        sheet.enable_bindings(
            "single_select", "row_select", "column_select", "arrowkeys",
            "copy", "select_all", "column_width_resize", "double_click_column_resize",
            "rc_select", "rc_popup_menu", "sort_rows",
        )
        sheet.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        shown = min(len(rows), 2000)
        suffix = f"{shown}+" if len(rows) > 2000 else str(shown)
        self.results_notebook.add(frame, text=f"Result {n}  ({suffix} rows)")
        self._result_sheets.append(sheet)
        self._populate_sheet(sheet, columns, rows)
        if select:
            self.results_notebook.select(frame)
        return sheet

    def _populate_sheet(self, sheet, columns, rows):
        shown_rows = rows[:2000]
        cell_font = tkfont.Font(font=("Consolas", 10))
        max_width_px = int(sheet.winfo_fpixels("2i"))
        min_width_px = 40
        padding_px = 24
        sample = shown_rows[:500]

        col_widths = []
        for i, col in enumerate(columns):
            header_w = cell_font.measure(str(col))
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
        if null_cells:
            sheet.highlight_cells(cells=null_cells, bg="#d9d9d9", fg="#999999", redraw=False)
        sheet.redraw()

    def _current_sheet(self):
        sel = self.results_notebook.select()
        if not sel:
            return None
        idx = self.results_notebook.index(sel)
        if idx < len(self._result_sheets):
            return self._result_sheets[idx]
        return None

    def export_csv(self):
        sheet = self._current_sheet()
        if sheet is None:
            messagebox.showinfo("Nothing to export", "Run a query that returns rows first.")
            return
        headers = sheet.headers()
        data = sheet.get_sheet_data()
        if not headers:
            messagebox.showinfo("Nothing to export", "Run a query that returns rows first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"{self.title}.csv",
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(data)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return

        self.app.set_status(self, f"Exported {len(data)} row(s) to {path}")

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
        self.current_file = None
        self.set_title(f"{self.TITLE_PREFIX} {self.index}")
        self._mark_clean()
        self._update_line_numbers()
        self.app._sync_chrome_if_active(self)
        self.app.set_status(self, "Tab reset.")

    def teardown(self):
        self._closed = True
        if self.find_dialog is not None and self.find_dialog.winfo_exists():
            self.find_dialog.destroy()
        self._hide_tooltip()
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
                "auth_mode": getattr(self, "_last_auth_mode", "windows"),
                "port": getattr(self, "_last_port", None),
                "encrypt": getattr(self, "_last_encrypt", True),
                "trust_cert": getattr(self, "_last_trust_cert", True),
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
        self.root.title("notepaDB — scratchpad for databases")
        self.root.geometry("1200x780")

        self.tabs = []
        self._tab_for_frame = {}
        self._tab_counter = 0
        self._menus = []

        options_cfg = load_config()
        self.dark_mode_var = tk.BooleanVar(value=options_cfg.get("dark_mode", False))
        self.show_line_numbers_var = tk.BooleanVar(value=options_cfg.get("show_line_numbers", True))
        self.word_wrap_var = tk.BooleanVar(value=options_cfg.get("word_wrap", False))
        self.restore_session_var = tk.BooleanVar(value=options_cfg.get("restore_session", False))

        self._native_ttk_theme = ttk.Style().theme_use()
        self._configure_dark_ttk_style()

        self._build_menu()
        self._build_toolbar()
        self._build_notebook()

        self._offer_crash_recovery()
        self._restore_or_new_tab()
        self._apply_global_ttk_theme()
        self._apply_chrome_theme()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30000, self._autosave_tick)

    @property
    def active_tab(self):
        if not self.tabs:
            return None
        sel = self.notebook.select()
        if not sel:
            return None
        frame = self.notebook.nametowidget(sel)
        return self._tab_for_frame.get(frame)

    def _dispatch(self, method_name, *args):
        tab = self.active_tab
        if tab is not None:
            getattr(tab, method_name)(*args)

    def set_status(self, tab, message):
        tab._log_message(message)

    def _sync_chrome_if_active(self, tab):
        if self.active_tab is tab:
            self._sync_chrome()

    def _sync_chrome(self):
        tab = self.active_tab
        if tab is None:
            return
        text, color = tab.connection_label()
        self.conn_label.configure(text=text, foreground=color)
        running = tab._query_running
        self.run_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    # ---------- options ----------

    def _save_option(self, key, value):
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)

    def toggle_dark_mode(self):
        self._save_option("dark_mode", self.dark_mode_var.get())
        self._apply_global_ttk_theme()
        self._apply_chrome_theme()
        for tab in self.tabs:
            tab.apply_theme(self.dark_mode_var.get())

    def toggle_line_numbers(self):
        self._save_option("show_line_numbers", self.show_line_numbers_var.get())
        for tab in self.tabs:
            tab.apply_line_numbers_visibility(self.show_line_numbers_var.get())

    def toggle_word_wrap(self):
        self._save_option("word_wrap", self.word_wrap_var.get())
        for tab in self.tabs:
            tab.apply_word_wrap(self.word_wrap_var.get())

    def toggle_restore_session(self):
        self._save_option("restore_session", self.restore_session_var.get())

    def open_history_dialog(self):
        if getattr(self, "_history_dialog", None) is not None and self._history_dialog.winfo_exists():
            self._history_dialog.history = load_history()
            self._history_dialog._populate()
            self._history_dialog.lift()
            self._history_dialog.focus_set()
            return
        self._history_dialog = HistoryDialog(self)

    def _configure_dark_ttk_style(self):
        # ttk's native "vista" theme on Windows ignores most color overrides
        # (it paints itself), so dark mode needs a themeable base — "clam" —
        # configured once here. Toggling just switches which theme is active;
        # the native theme's own look is never touched, so light mode always
        # restores it exactly.
        bg, bg_alt, fg, sel = "#1e1e1e", "#2d2d2d", "#d4d4d4", "#094771"
        style = ttk.Style()
        if "notepadb_dark" not in style.theme_names():
            style.theme_create("notepadb_dark", parent="clam")
        style.theme_use("notepadb_dark")
        style.configure(".", background=bg, foreground=fg, fieldbackground=bg_alt, bordercolor="#3c3c3c")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background=bg_alt, foreground=fg)
        style.map("TButton", background=[("active", "#3c3c3c")])
        style.configure("TEntry", fieldbackground=bg_alt, foreground=fg, insertcolor=fg)
        style.configure("TCombobox", fieldbackground=bg_alt, foreground=fg, background=bg_alt, arrowcolor=fg)
        style.map("TCombobox", fieldbackground=[("readonly", bg_alt)], foreground=[("readonly", fg)])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.map("TCheckbutton", background=[("active", bg)])
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TPanedwindow", background=bg)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=bg_alt, foreground=fg, padding=(8, 4))
        style.map("TNotebook.Tab", background=[("selected", sel)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview", background=bg_alt, fieldbackground=bg_alt, foreground=fg)
        style.map("Treeview", background=[("selected", sel)], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=bg_alt, foreground=fg)
        style.configure("TSeparator", background="#3c3c3c")
        style.configure("TScrollbar", background=bg_alt, troughcolor=bg, arrowcolor=fg)
        style.theme_use(self._native_ttk_theme)  # leave it as it was until toggled on

    def _apply_global_ttk_theme(self):
        style = ttk.Style()
        style.theme_use("notepadb_dark" if self.dark_mode_var.get() else self._native_ttk_theme)

    def _apply_chrome_theme(self):
        dark = self.dark_mode_var.get()
        if dark:
            root_bg = "#1e1e1e"
            menu_colors = dict(
                bg="#2d2d2d", fg="#d4d4d4", activebackground="#094771", activeforeground="#ffffff",
            )
        else:
            root_bg = "SystemButtonFace"
            menu_colors = dict(
                bg="SystemMenu", fg="SystemMenuText",
                activebackground="SystemHighlight", activeforeground="SystemHighlightText",
            )
        try:
            self.root.configure(background=root_bg)
        except tk.TclError:
            pass
        for menu in self._menus:
            try:
                menu.configure(**menu_colors)
            except tk.TclError:
                pass

    # ---------- UI construction ----------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New Tab", command=self.new_tab, accelerator="Ctrl+T")
        file_menu.add_command(label="Open...", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Save", command=lambda: self._dispatch("save_file"), accelerator="Ctrl+S")
        file_menu.add_command(label="Save As...", command=lambda: self._dispatch("save_file_as"))
        file_menu.add_separator()
        file_menu.add_command(label="Close Tab", command=self.close_tab, accelerator="Ctrl+W")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Toggle Comment", command=lambda: self._dispatch("toggle_comment"), accelerator="Ctrl+/")
        menubar.add_cascade(label="Edit", menu=edit_menu)

        conn_menu = tk.Menu(menubar, tearoff=0)
        conn_menu.add_command(label="Connect...", command=lambda: self._dispatch("open_connect_dialog"), accelerator="Ctrl+K")
        conn_menu.add_command(label="Disconnect", command=lambda: self._dispatch("disconnect"))
        conn_menu.add_command(label="Refresh Schema", command=lambda: self._dispatch("refresh_schema"))
        menubar.add_cascade(label="Connection", menu=conn_menu)

        find_menu = tk.Menu(menubar, tearoff=0)
        find_menu.add_command(label="Find...", command=lambda: self._dispatch("open_find_dialog"), accelerator="Ctrl+F")
        find_menu.add_command(label="Find Next", command=lambda: self._dispatch("find_next"), accelerator="F3")
        find_menu.add_command(label="Find Previous", command=lambda: self._dispatch("find_previous"), accelerator="Shift+F3")
        find_menu.add_separator()
        find_menu.add_command(label="Replace...", command=lambda: self._dispatch("open_replace_dialog"), accelerator="Ctrl+H")
        find_menu.add_command(label="Replace All", command=lambda: self._dispatch("replace_all"))
        menubar.add_cascade(label="Find", menu=find_menu)

        query_menu = tk.Menu(menubar, tearoff=0)
        query_menu.add_command(label="Run", command=lambda: self._dispatch("run_query"), accelerator="F5")
        query_menu.add_command(label="Cancel", command=lambda: self._dispatch("cancel_query"), accelerator="Ctrl+Break")
        query_menu.add_command(label="Explain Plan", command=lambda: self._dispatch("explain_query"), accelerator="Ctrl+L")
        query_menu.add_separator()
        query_menu.add_command(label="Format SQL", command=lambda: self._dispatch("format_sql"), accelerator="Ctrl+Shift+F")
        query_menu.add_command(label="History...", command=self.open_history_dialog, accelerator="Ctrl+Shift+H")
        query_menu.add_separator()
        query_menu.add_command(label="Export Results to CSV...", command=lambda: self._dispatch("export_csv"))
        menubar.add_cascade(label="Query", menu=query_menu)

        options_menu = tk.Menu(menubar, tearoff=0)
        options_menu.add_checkbutton(
            label="Dark Mode", variable=self.dark_mode_var, command=self.toggle_dark_mode,
        )
        options_menu.add_checkbutton(
            label="Show Line Numbers", variable=self.show_line_numbers_var, command=self.toggle_line_numbers,
        )
        options_menu.add_checkbutton(
            label="Word Wrap", variable=self.word_wrap_var, command=self.toggle_word_wrap,
        )
        options_menu.add_checkbutton(
            label="Restore Tabs on Startup", variable=self.restore_session_var, command=self.toggle_restore_session,
        )
        menubar.add_cascade(label="Options", menu=options_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self._menus = [
            menubar, file_menu, edit_menu, conn_menu, find_menu, query_menu, options_menu, help_menu,
        ]

        self.root.config(menu=menubar)
        self.root.bind("<Control-t>", lambda e: self.new_tab())
        self.root.bind("<Control-n>", lambda e: self.new_tab())
        self.root.bind("<Control-w>", lambda e: self.close_tab())
        self.root.bind("<Control-o>", lambda e: self.open_file())
        self.root.bind("<Control-s>", lambda e: self._dispatch("save_file"))
        self.root.bind("<Control-k>", lambda e: self._dispatch("open_connect_dialog"))
        self.root.bind("<F5>", lambda e: self._dispatch("run_query"))
        self.root.bind("<Control-Cancel>", lambda e: self._dispatch("cancel_query"))
        self.root.bind("<Control-l>", lambda e: self._dispatch("explain_query"))
        self.root.bind("<Control-Shift-F>", lambda e: self._dispatch("format_sql"))
        self.root.bind("<Control-Shift-H>", lambda e: self.open_history_dialog())
        self.root.bind("<Control-f>", lambda e: self._dispatch("open_find_dialog"))
        self.root.bind("<Control-h>", lambda e: self._dispatch("open_replace_dialog"))
        self.root.bind("<F3>", lambda e: self._dispatch("find_next"))
        self.root.bind("<Shift-F3>", lambda e: self._dispatch("find_previous"))

    def _build_toolbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x")
        self.run_btn = ttk.Button(bar, text="Run  (F5)", command=lambda: self._dispatch("run_query"))
        self.run_btn.pack(side="left", padx=4, pady=4)
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=lambda: self._dispatch("cancel_query"), state="disabled")
        self.cancel_btn.pack(side="left", padx=4, pady=4)

        self.conn_label = ttk.Label(bar, text="Not connected", foreground="#b00020")
        self.conn_label.pack(side="right", padx=10)

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(side="top", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._sync_chrome())
        self.notebook.bind("<Button-3>", self._on_tab_right_click)

    # ---------- tab management ----------

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

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("SQL files", "*.sql"), ("All files", "*.*")])
        if not path:
            return
        tab = self.new_tab()
        tab.load_file(path)

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

    def _confirm_close(self, tab):
        """True if it's OK to proceed closing/discarding `tab` — prompts to
        save first if it has unsaved changes. False means the caller should
        abort (either the user hit Cancel, or a Save-As they triggered was
        itself cancelled)."""
        if not tab.dirty:
            return True
        self.notebook.select(tab.frame)
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            f"'{tab.title}' has unsaved changes. Save before closing?",
        )
        if answer is None:
            return False
        if answer:
            tab.save_file()
            if tab.dirty:
                return False
        return True

    def _on_tab_right_click(self, event):
        try:
            idx = self.notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        frame = self.notebook.nametowidget(self.notebook.tabs()[idx])
        tab = self._tab_for_frame.get(frame)
        if tab is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Close Tab", command=lambda: self.close_tab(tab))
        menu.tk_popup(event.x_root, event.y_root)

    # ---------- misc ----------

    def _show_about(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("About notepaDB")
        dlg.resizable(False, False)
        dlg.transient(self.root)

        frame = ttk.Frame(dlg)
        frame.pack(fill="both", expand=True, padx=20, pady=16)
        ttk.Label(
            frame,
            text=(
                "notepaDB — a notepad, but only for databases.\n\n"
                "Type SQL, autocomplete real table/column names, run it.\n\n"
                "Each tab is an independent session with its own connection.\n\n"
                "Supported engines: SQL Server, PostgreSQL, MySQL/MariaDB, Oracle, SQLite."
            ),
            justify="left",
        ).pack(anchor="w")
        link = ttk.Label(frame, text="Buy me a coffee", foreground="#1A73E8", cursor="hand2", font=("Segoe UI", 9, "underline"))
        link.pack(anchor="w", pady=(16, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open("ko-fi.com/drormaor"))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(16, 0))
        close_btn = ttk.Button(btn_frame, text="Close", command=dlg.destroy)
        close_btn.pack(side="right")

        dlg.bind("<Return>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.after_idle(close_btn.focus_set)
        dlg.grab_set()

    def _on_close(self):
        for tab in self.tabs:
            if not self._confirm_close(tab):
                return
        save_session([tab.session_state() for tab in self.tabs])
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
            tab.highlight_syntax()
            tab._update_line_numbers()

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
            tab._log_message(f"Session restored — reconnect manually to {label} (password not saved).")

    def _offer_crash_recovery(self):
        files = _list_autosave_files()
        if not files:
            return
        if not messagebox.askyesno(
            "Recover unsaved work?",
            f"notepaDB didn't close cleanly last time — {len(files)} tab(s) with unsaved "
            "changes were found. Recover them?",
        ):
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
            tab.highlight_syntax()
            tab._update_line_numbers()
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
    root = tk.Tk()
    try:
        root.state("zoomed")
    except tk.TclError:
        root.attributes("-zoomed", True)
    app = NotepaDBApp(root)
    tab = app.active_tab
    tab._update_line_numbers()
    root.mainloop()


if __name__ == "__main__":
    main()
