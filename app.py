"""
notepadb — a notepad/scratchpad, but only for databases.

Type SQL, get autocomplete for real table/column names from the server
you're connected to, run it, see results. SQL Server works today;
PostgreSQL and MySQL/MariaDB are stubbed in db_backends.py for later.

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


class Autocomplete:
    """Floating suggestion popup for the SQL editor."""

    def __init__(self, app):
        self.app = app
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
        engine_box = ttk.Combobox(
            frame, textvariable=self.engine_var, state="readonly",
            values=["sqlserver", "postgres", "mysql"], width=28,
        )
        engine_box.grid(row=row, column=1, sticky="w", **pad)

        def fmt_engine(name):
            return {
                "sqlserver": "SQL Server",
                "postgres": "PostgreSQL (coming soon)",
                "mysql": "MySQL / MariaDB (coming soon)",
            }[name]

        engine_box["values"] = ["sqlserver", "postgres", "mysql"]
        engine_box.configure(postcommand=lambda: None)
        # Show friendly labels but keep engine keys underneath.
        self._engine_display_map = {fmt_engine(k): k for k in ["sqlserver", "postgres", "mysql"]}
        engine_box["values"] = list(self._engine_display_map.keys())
        engine_box.set(fmt_engine(self.engine_var.get()))

        def on_engine_pick(event=None):
            display = engine_box.get()
            self.engine_var.set(self._engine_display_map[display])

        engine_box.bind("<<ComboboxSelected>>", on_engine_pick)
        row += 1

        ttk.Label(frame, text="Server:").grid(row=row, column=0, sticky="e", **pad)
        self.server_var = tk.StringVar(value=defaults.get("server", "localhost"))
        ttk.Entry(frame, textvariable=self.server_var, width=30).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Port (optional):").grid(row=row, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar(value=defaults.get("port", ""))
        ttk.Entry(frame, textvariable=self.port_var, width=30).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(frame, text="Database:").grid(row=row, column=0, sticky="e", **pad)
        self.database_var = tk.StringVar(value=defaults.get("database", ""))
        ttk.Entry(frame, textvariable=self.database_var, width=30).grid(row=row, column=1, sticky="w", **pad)
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
        if engine != "sqlserver":
            self.status_label.configure(text="That engine isn't implemented yet — pick SQL Server.")
            return
        if not self.server_var.get().strip():
            self.status_label.configure(text="Server is required.")
            return
        if not self.database_var.get().strip():
            self.status_label.configure(text="Database is required.")
            return
        self.result = {
            "engine": engine,
            "server": self.server_var.get().strip(),
            "port": self.port_var.get().strip() or None,
            "database": self.database_var.get().strip(),
            "auth_mode": self.auth_var.get(),
            "username": self.username_var.get().strip(),
            "password": self.password_var.get(),
            "encrypt": self.encrypt_var.get(),
            "trust_cert": self.trust_var.get(),
        }
        self.destroy()


class NotepadDBApp:
    def __init__(self, root):
        self.root = root
        self.root.title("notepaDB — scratchpad for databases")
        self.root.geometry("1200x780")

        self.backend = None
        self.schema = None  # {'schema.table': [col, ...]}
        self.current_file = None
        self._work_queue = queue.Queue()

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.autocomplete = Autocomplete(self)
        self._bind_editor_events()
        self.highlight_syntax()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

    # ---------- UI construction ----------

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="New", command=self.new_file, accelerator="Ctrl+N")
        file_menu.add_command(label="Open...", command=self.open_file, accelerator="Ctrl+O")
        file_menu.add_command(label="Save", command=self.save_file, accelerator="Ctrl+S")
        file_menu.add_command(label="Save As...", command=self.save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        conn_menu = tk.Menu(menubar, tearoff=0)
        conn_menu.add_command(label="Connect...", command=self.open_connect_dialog, accelerator="Ctrl+K")
        conn_menu.add_command(label="Disconnect", command=self.disconnect)
        conn_menu.add_command(label="Refresh Schema", command=self.refresh_schema)
        menubar.add_cascade(label="Connection", menu=conn_menu)

        query_menu = tk.Menu(menubar, tearoff=0)
        query_menu.add_command(label="Run", command=self.run_query, accelerator="F5")
        menubar.add_cascade(label="Query", menu=query_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)
        self.root.bind("<Control-n>", lambda e: self.new_file())
        self.root.bind("<Control-o>", lambda e: self.open_file())
        self.root.bind("<Control-s>", lambda e: self.save_file())
        self.root.bind("<Control-k>", lambda e: self.open_connect_dialog())
        self.root.bind("<F5>", lambda e: self.run_query())

    def _build_toolbar(self):
        bar = ttk.Frame(self.root)
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="Connect...", command=self.open_connect_dialog).pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Disconnect", command=self.disconnect).pack(side="left", padx=4, pady=4)
        ttk.Button(bar, text="Refresh Schema", command=self.refresh_schema).pack(side="left", padx=4, pady=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6, pady=4)
        ttk.Button(bar, text="Run  (F5)", command=self.run_query).pack(side="left", padx=4, pady=4)

        self.conn_label = ttk.Label(bar, text="Not connected", foreground="#b00020")
        self.conn_label.pack(side="right", padx=10)

    def _build_body(self):
        main = ttk.PanedWindow(self.root, orient="horizontal")
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
        self.schema_tree.bind("<Double-Button-1>", self._on_tree_double_click)
        self.schema_tree.bind("<ButtonPress-1>", self._on_tree_press, add="+")
        self.schema_tree.bind("<B1-Motion>", self._on_tree_motion, add="+")
        self.schema_tree.bind("<ButtonRelease-1>", self._on_tree_release, add="+")
        main.add(left, weight=0)

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

        self._configure_syntax_tags()

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Ready.")
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True, padx=6, pady=2)

    def _configure_syntax_tags(self):
        self.editor.tag_configure("kw", foreground="#0000cc", font=("Consolas", 11, "bold"))
        self.editor.tag_configure("str", foreground="#a31515")
        self.editor.tag_configure("comment", foreground="#008000")
        self.editor.tag_configure("num", foreground="#098658")

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
        self.schema_tree.delete(*self.schema_tree.get_children())
        if not self.schema:
            return
        filt = self.filter_var.get().strip().lower()
        for table_key in sorted(self.schema.keys()):
            cols = self.schema[table_key]
            if filt and filt not in table_key.lower() and not any(filt in c.lower() for c in cols):
                continue
            table_node = self.schema_tree.insert("", "end", text=table_key, values=(table_key,), open=bool(filt))
            for col in cols:
                if filt and filt not in table_key.lower() and filt not in col.lower():
                    continue
                self.schema_tree.insert(table_node, "end", text=col, values=(f"{table_key}.{col}",))

    def _on_tree_double_click(self, event):
        item = self.schema_tree.focus()
        if not item:
            return
        values = self.schema_tree.item(item, "values")
        if not values:
            return
        text_to_insert = self.schema_tree.item(item, "text")
        self.editor.insert(tk.INSERT, text_to_insert)
        self.highlight_syntax()
        self.editor.focus_set()

    # ---------- drag-and-drop: schema tree -> editor ----------

    DRAG_THRESHOLD_PX = 4

    def _on_tree_press(self, event):
        item = self.schema_tree.identify_row(event.y)
        if not item or not self.schema_tree.item(item, "values"):
            self._drag_data = None
            return
        self._drag_data = {
            "text": self.schema_tree.item(item, "text"),
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

    def new_file(self):
        if self.editor.get("1.0", "end-1c").strip():
            if not messagebox.askyesno("New file", "Discard current contents?"):
                return
        self.editor.delete("1.0", "end")
        self.current_file = None
        self._update_line_numbers()
        self.status_var.set("New scratchpad.")

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("SQL files", "*.sql"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", content)
        self.current_file = path
        self.highlight_syntax()
        self._update_line_numbers()
        self.status_var.set(f"Opened {path}")

    def save_file(self):
        if not self.current_file:
            return self.save_file_as()
        with open(self.current_file, "w", encoding="utf-8") as f:
            f.write(self.editor.get("1.0", "end-1c"))
        self.status_var.set(f"Saved {self.current_file}")

    def save_file_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".sql", filetypes=[("SQL files", "*.sql")])
        if not path:
            return
        self.current_file = path
        self.save_file()

    # ---------- connection ----------

    def open_connect_dialog(self):
        defaults = load_config()
        dialog = ConnectDialog(self.root, defaults)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        self._connect_with(dialog.result)

    def _connect_with(self, params):
        self.status_var.set(f"Connecting to {params['server']}/{params['database']}...")
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
            schema = backend.list_schema()
            return backend, schema, params

        def done(result, error):
            if error is not None:
                self.status_var.set("Connection failed.")
                messagebox.showerror("Connection failed", str(error))
                return
            backend, schema, params = result
            self.backend = backend
            self.schema = schema
            self.populate_schema_tree()
            self.conn_label.configure(
                text=f"Connected: {params['server']}/{params['database']} ({backend.label})",
                foreground="#0a7a0a",
            )
            self.status_var.set(f"Connected. {len(schema)} tables loaded.")
            to_save = dict(params)
            to_save.pop("password", None)
            save_config(to_save)

        self._run_async(work, done)

    def disconnect(self):
        if self.backend is not None:
            self.backend.disconnect()
        self.backend = None
        self.schema = None
        self.populate_schema_tree()
        self.conn_label.configure(text="Not connected", foreground="#b00020")
        self.status_var.set("Disconnected.")

    def refresh_schema(self):
        if self.backend is None or not self.backend.is_connected:
            messagebox.showinfo("Not connected", "Connect to a database first.")
            return
        self.status_var.set("Refreshing schema...")

        def work():
            return self.backend.list_schema()

        def done(result, error):
            if error is not None:
                self.status_var.set("Schema refresh failed.")
                messagebox.showerror("Schema refresh failed", str(error))
                return
            self.schema = result
            self.populate_schema_tree()
            self.status_var.set(f"Schema refreshed. {len(result)} tables.")

        self._run_async(work, done)

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
            self.status_var.set("Nothing to run.")
            return

        self.status_var.set("Running query...")
        started = time.monotonic()

        def work():
            return self.backend.execute(sql)

        def done(result, error):
            elapsed_ms = (time.monotonic() - started) * 1000
            if error is not None:
                self.status_var.set(f"Error after {elapsed_ms:.0f} ms.")
                messagebox.showerror("Query error", str(error))
                return
            columns, rows = result
            if columns is None:
                affected = rows
                self.status_var.set(f"OK. {affected} row(s) affected. ({elapsed_ms:.0f} ms)")
                self.results_sheet.headers([])
                self.results_sheet.set_sheet_data([], reset_highlights=True)
                return
            self._show_results(columns, rows)
            truncated = len(rows) > 2000
            shown = min(len(rows), 2000)
            self.status_var.set(
                f"{len(rows)} row(s) returned ({elapsed_ms:.0f} ms)"
                + (f" — showing first {shown}" if truncated else "")
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
        self.root.after(100, self._poll_queue)

    # ---------- misc ----------

    def _show_about(self):
        messagebox.showinfo(
            "About notepaDB",
            "notepaDB — a notepad, but only for databases.\n\n"
            "Type SQL, autocomplete real table/column names, run it.\n\n"
            "Supported today: SQL Server.\n"
            "Coming later: PostgreSQL, MySQL/MariaDB.",
        )

    def _on_close(self):
        if self.backend is not None:
            self.backend.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.state("zoomed")
    except tk.TclError:
        root.attributes("-zoomed", True)
    app = NotepadDBApp(root)
    app.editor.insert("1.0", "-- Connect (Ctrl+K), then start typing, e.g.:\n-- SELECT * FROM \n")
    app._update_line_numbers()
    root.mainloop()


if __name__ == "__main__":
    main()
