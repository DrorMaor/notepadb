"""
notepadb — a notepad/scratchpad, but only for databases.

Type SQL, get autocomplete for real table/column names from the server
you're connected to, run it, see results. SQL Server, PostgreSQL, and
MySQL/MariaDB are all supported via db_backends.py.

Tabs are independent sessions (like SSMS query windows): each tab has its
own connection, schema browser, and results grid.

Run with:  py app.py
"""

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont

from tksheet import Sheet

from db_backends import BACKENDS, BackendError
from autocomplete import get_suggestions, SQL_KEYWORDS

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, ".notepadb_config.json")

KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(sorted({kw.split()[0] for kw in SQL_KEYWORDS}, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
STRING_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'")
LINE_COMMENT_PATTERN = re.compile(r"--.*")
BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
NUMBER_PATTERN = re.compile(r"\b\d+(\.\d+)?\b")


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

        def fmt_engine(name):
            return {
                "sqlserver": "SQL Server",
                "postgres": "PostgreSQL",
                "mysql": "MySQL / MariaDB",
            }[name]

        # Show friendly labels but keep engine keys underneath.
        self._engine_display_map = {fmt_engine(k): k for k in ["sqlserver", "postgres", "mysql"]}
        engine_box["values"] = list(self._engine_display_map.keys())
        self._engine_display_var.set(fmt_engine(self.engine_var.get()))

        def on_engine_pick(event=None):
            display = self._engine_display_var.get()
            self.engine_var.set(self._engine_display_map[display])

        engine_box.bind("<<ComboboxSelected>>", on_engine_pick)
        row += 1

        ttk.Label(frame, text="Server:").grid(row=row, column=0, sticky="e", **pad)
        self.server_var = tk.StringVar(value=defaults.get("server", "localhost"))
        server_box = ttk.Combobox(
            frame, textvariable=self.server_var, width=28,
            values=defaults.get("recent_servers", []),
        )
        server_box.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Port (optional):").grid(row=row, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar(value=defaults.get("port", ""))
        ttk.Entry(frame, textvariable=self.port_var, width=30).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Database (optional):").grid(row=row, column=0, sticky="e", **pad)
        self.database_var = tk.StringVar(value=defaults.get("database", ""))
        database_box = ttk.Combobox(
            frame, textvariable=self.database_var, width=28,
            values=defaults.get("recent_databases", []),
        )
        database_box.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Authentication:").grid(row=row, column=0, sticky="e", **pad)
        self.auth_var = tk.StringVar(value=defaults.get("auth_mode", "windows"))
        auth_frame = ttk.Frame(frame)
        auth_frame.grid(row=row, column=1, sticky="w", **pad)
        ttk.Radiobutton(auth_frame, text="Windows", variable=self.auth_var, value="windows",
                        command=self._sync_auth_fields).pack(side="left")
        ttk.Radiobutton(auth_frame, text="SQL Login", variable=self.auth_var, value="sql",
                        command=self._sync_auth_fields).pack(side="left")
        row += 1

        ttk.Label(frame, text="Username:").grid(row=row, column=0, sticky="e", **pad)
        self.username_var = tk.StringVar(value=defaults.get("username", ""))
        self.username_entry = ttk.Entry(frame, textvariable=self.username_var, width=30)
        self.username_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Password:").grid(row=row, column=0, sticky="e", **pad)
        self.password_var = tk.StringVar(value="")
        self.password_entry = ttk.Entry(frame, textvariable=self.password_var, width=30, show="*")
        self.password_entry.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        opts_frame = ttk.Frame(frame)
        opts_frame.grid(row=row, column=1, sticky="w", **pad)
        self.encrypt_var = tk.BooleanVar(value=defaults.get("encrypt", True))
        self.trust_var = tk.BooleanVar(value=defaults.get("trust_cert", True))
        ttk.Checkbutton(opts_frame, text="Encrypt", variable=self.encrypt_var).pack(side="left")
        ttk.Checkbutton(opts_frame, text="Trust server certificate", variable=self.trust_var).pack(side="left")
        row += 1

        self.status_label = ttk.Label(frame, text="", foreground="#b00020")
        self.status_label.grid(row=row, column=0, columnspan=2, sticky="w", padx=8)
        row += 1

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=(10, 0))
        self.connect_btn = ttk.Button(btn_frame, text="Connect", command=self._connect)
        self.connect_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side="left", padx=4)

        self._sync_auth_fields()

    def _sync_auth_fields(self):
        is_sql = self.auth_var.get() == "sql"
        state = "normal" if is_sql else "disabled"
        self.username_entry.configure(state=state)
        self.password_entry.configure(state=state)

    def _cancel(self):
        self.result = None
        self.destroy()

    def _connect(self):
        engine = self.engine_var.get()
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

        self.frame = ttk.Frame(app.notebook)
        self._build_body()
        self.apply_theme(self.app.dark_mode_var.get())
        self.apply_word_wrap(self.app.word_wrap_var.get())
        self.apply_line_numbers_visibility(self.app.show_line_numbers_var.get())

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

        # --- right: editor + results, stacked vertically ---
        right = ttk.PanedWindow(main, orient="vertical")
        main.add(right, weight=1)

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
        self.results_sheet = Sheet(
            results_frame,
            headers=[],
            data=[],
            font=("Consolas", 10, "normal"),
            header_font=("Consolas", 10, "bold"),
            theme="light blue",
        )
        self.results_sheet.enable_bindings(
            "single_select", "row_select", "column_select", "arrowkeys",
            "copy", "select_all", "column_width_resize", "double_click_column_resize",
            "rc_select",
        )
        self.results_sheet.grid(row=0, column=0, sticky="nsew")
        results_frame.rowconfigure(0, weight=1)
        results_frame.columnconfigure(0, weight=1)
        right.add(results_frame, weight=2)

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
        else:
            editor_bg, editor_fg, editor_insert = "#ffffff", "#000000", "black"
            ln_bg, ln_fg = "#f0f0f0", "#000000"
            kw_c, str_c, comment_c, num_c = "#0000cc", "#a31515", "#008000", "#098658"
            sheet_theme = "light blue"

        self.editor.configure(background=editor_bg, foreground=editor_fg, insertbackground=editor_insert)
        self.line_numbers.configure(background=ln_bg, foreground=ln_fg)
        self.editor.tag_configure("kw", foreground=kw_c, font=("Consolas", 11, "bold"))
        self.editor.tag_configure("str", foreground=str_c)
        self.editor.tag_configure("comment", foreground=comment_c)
        self.editor.tag_configure("num", foreground=num_c)
        try:
            self.results_sheet.change_theme(sheet_theme)
        except Exception:
            pass

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
        self.editor.bind("<<Modified>>", self._on_modified_flag)

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
            return
        self.highlight_syntax()
        self._update_line_numbers()
        self.autocomplete.update_suggestions()

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
            table_node = self.schema_tree.insert(db_node, "end", text=table_key, values=(table_key,), open=bool(filt))
            for col_name, col_type in cols:
                if filt and filt not in table_key.lower() and filt not in col_name.lower():
                    continue
                self.schema_tree.insert(
                    table_node, "end", text=f"{col_name}  {col_type}", values=(col_name,),
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
        if not item or "database" not in self.schema_tree.item(item, "tags"):
            return
        self.schema_tree.selection_set(item)
        self.schema_tree.focus(item)
        db_name = self._tree_item_label(item)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=f"Use '{db_name}'", command=lambda: self._set_active_database(db_name))
        menu.tk_popup(event.x_root, event.y_root)

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
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
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

    # ---------- query execution ----------

    def run_query(self):
        if self.backend is None or not self.backend.is_connected:
            messagebox.showinfo("Not connected", "Connect to a database first.")
            return
        try:
            sel_start, sel_end = self.editor.index("sel.first"), self.editor.index("sel.last")
            sql = self.editor.get(sel_start, sel_end)
        except tk.TclError:
            sql = self.editor.get("1.0", "end-1c")
        sql = sql.strip()
        if not sql:
            self.app.set_status(self, "Nothing to run.")
            return

        self.app.set_status(self, "Running query...")
        started = time.monotonic()

        def work():
            return self.backend.execute(sql)

        def done(result, error):
            elapsed_ms = (time.monotonic() - started) * 1000
            if error is not None:
                self.app.set_status(self, f"Error after {elapsed_ms:.0f} ms.")
                messagebox.showerror("Query error", str(error))
                return
            columns, rows = result
            if columns is None:
                affected = rows
                self.app.set_status(self, f"OK. {affected} row(s) affected. ({elapsed_ms:.0f} ms)")
                self.results_sheet.headers([])
                self.results_sheet.set_sheet_data([], reset_highlights=True)
                return
            self._show_results(columns, rows)
            truncated = len(rows) > 2000
            shown = min(len(rows), 2000)
            self.app.set_status(
                self,
                f"{len(rows)} row(s) returned ({elapsed_ms:.0f} ms)"
                + (f" — showing first {shown}" if truncated else ""),
            )

        self._run_async(work, done)

    def _show_results(self, columns, rows):
        shown_rows = rows[:2000]
        cell_font = tkfont.Font(font=("Consolas", 10))
        max_width_px = int(self.results_sheet.winfo_fpixels("2i"))
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

        self.results_sheet.headers(list(columns))
        self.results_sheet.set_sheet_data(display_rows, reset_highlights=True)
        self.results_sheet.set_column_widths(col_widths)
        if null_cells:
            self.results_sheet.highlight_cells(
                cells=null_cells, bg="#d9d9d9", fg="#999999", redraw=False
            )
        self.results_sheet.redraw()

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


class NotepaDBApp:
    def __init__(self, root):
        self.root = root
        self.root.title("notepaDB — scratchpad for databases")
        self.root.geometry("1200x780")

        self.status_var = tk.StringVar(value="Ready.")
        self.tabs = []
        self._tab_for_frame = {}
        self._tab_counter = 0
        self._menus = []

        options_cfg = load_config()
        self.dark_mode_var = tk.BooleanVar(value=options_cfg.get("dark_mode", False))
        self.show_line_numbers_var = tk.BooleanVar(value=options_cfg.get("show_line_numbers", True))
        self.word_wrap_var = tk.BooleanVar(value=options_cfg.get("word_wrap", False))

        self._native_ttk_theme = ttk.Style().theme_use()
        self._configure_dark_ttk_style()

        self._build_menu()
        self._build_toolbar()
        self._build_notebook()
        self._build_statusbar()

        self.new_tab()
        self._apply_global_ttk_theme()
        self._apply_chrome_theme()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        if self.active_tab is tab:
            self.status_var.set(message)
        else:
            self.status_var.set(f"[{tab.title}] {message}")

    def _sync_chrome_if_active(self, tab):
        if self.active_tab is tab:
            self._sync_chrome()

    def _sync_chrome(self):
        tab = self.active_tab
        if tab is None:
            return
        text, color = tab.connection_label()
        self.conn_label.configure(text=text, foreground=color)

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
        menubar.add_cascade(label="Options", menu=options_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self._menus = [
            menubar, file_menu, conn_menu, find_menu, query_menu, options_menu, help_menu,
        ]

        self.root.config(menu=menubar)
        self.root.bind("<Control-t>", lambda e: self.new_tab())
        self.root.bind("<Control-n>", lambda e: self.new_tab())
        self.root.bind("<Control-w>", lambda e: self.close_tab())
        self.root.bind("<Control-o>", lambda e: self.open_file())
        self.root.bind("<Control-s>", lambda e: self._dispatch("save_file"))
        self.root.bind("<Control-k>", lambda e: self._dispatch("open_connect_dialog"))
        self.root.bind("<F5>", lambda e: self._dispatch("run_query"))
        self.root.bind("<Control-f>", lambda e: self._dispatch("open_find_dialog"))
        self.root.bind("<Control-h>", lambda e: self._dispatch("open_replace_dialog"))
        self.root.bind("<F3>", lambda e: self._dispatch("find_next"))
        self.root.bind("<Shift-F3>", lambda e: self._dispatch("find_previous"))

    def _build_toolbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="New Tab", command=self.new_tab).pack(side="left", padx=4, pady=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)
        ttk.Button(bar, text="Connect...", command=lambda: self._dispatch("open_connect_dialog")).pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Disconnect", command=lambda: self._dispatch("disconnect")).pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Refresh Schema", command=lambda: self._dispatch("refresh_schema")).pack(side="left", padx=4, pady=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)
        ttk.Button(bar, text="Run  (F5)", command=lambda: self._dispatch("run_query")).pack(side="left", padx=4, pady=4)

        self.conn_label = ttk.Label(bar, text="Not connected", foreground="#b00020")
        self.conn_label.pack(side="right", padx=10)

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(side="top", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._sync_chrome())
        self.notebook.bind("<Button-3>", self._on_tab_right_click)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True, padx=6, pady=2)

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
        messagebox.showinfo(
            "About notepaDB",
            "notepaDB — a notepad, but only for databases.\n\n"
            "Type SQL, autocomplete real table/column names, run it.\n\n"
            "Each tab is an independent session with its own connection.\n\n"
            "Supported engines: SQL Server, PostgreSQL, MySQL/MariaDB.",
        )

    def _on_close(self):
        for tab in self.tabs:
            if not self._confirm_close(tab):
                return
        for tab in self.tabs:
            if tab.backend is not None:
                tab.backend.disconnect()
        self.root.destroy()


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
