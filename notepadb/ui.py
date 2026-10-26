# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""
Reusable themed building blocks for notepaDB: dialogs that look like the
rest of the app (no stock Tk message boxes), tooltips, an inline find bar,
a command palette, the status bar, empty-state panels, and file pickers
that use the desktop's own chooser where one is available.

Everything here draws from the active `theme.Theme`, installed once with
`ui.install(theme)`.
"""

import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog

_theme = None


def install(theme):
    global _theme
    _theme = theme


def current():
    return _theme


def px(n):
    return _theme.px(n)


# =============================================================================
# Geometry helpers
# =============================================================================

def center_over(win, parent=None):
    """Position `win` centred over `parent` (or the screen), clamped on-screen."""
    win.update_idletasks()
    w = win.winfo_reqwidth()
    h = win.winfo_reqheight()
    if parent is not None and parent.winfo_viewable():
        px_ = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        py_ = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    else:
        px_ = (win.winfo_screenwidth() - w) // 2
        py_ = (win.winfo_screenheight() - h) // 2
    px_ = max(0, min(px_, win.winfo_screenwidth() - w))
    py_ = max(0, min(py_, win.winfo_screenheight() - h - px(40)))
    win.geometry(f"+{px_}+{py_}")


def _focused_is_text(widget):
    try:
        focus = widget.focus_get()
    except (KeyError, tk.TclError):
        return False
    return isinstance(focus, tk.Text)


# =============================================================================
# Dialog base
# =============================================================================

class Dialog(tk.Toplevel):
    """A themed dialog: padded body, right-aligned button row, Esc cancels,
    Enter triggers the default button, centred over its parent."""

    def __init__(self, parent, title, modal=True, resizable=(False, False), padding=18):
        super().__init__(parent)
        self.withdraw()
        th = current()
        self._parent = parent.winfo_toplevel() if parent is not None else None
        self.configure(background=th.pal.dialog)
        self.title(title)
        if self._parent is not None:
            self.transient(self._parent)
        self.resizable(*resizable)
        self.result = None
        self._modal = modal
        self._default_btn = None
        self._buttons = []

        self.outer = ttk.Frame(self, style="Dialog.TFrame", padding=px(padding))
        self.outer.pack(fill="both", expand=True)
        # Buttons are packed first (bottom) so a window that ends up shorter
        # than requested squeezes the body, never the buttons.
        self.buttons = ttk.Frame(self.outer, style="Dialog.TFrame")
        self.buttons.pack(side="bottom", fill="x", pady=(px(18), 0))
        self.body = ttk.Frame(self.outer, style="Dialog.TFrame")
        self.body.pack(side="top", fill="both", expand=True)

        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Return>", self._on_return)
        self.bind("<KP_Enter>", self._on_return)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def add_button(self, text, command, kind="normal", default=False, side="right", width=None):
        style = {"normal": "TButton", "accent": "Accent.TButton", "destructive": "Destructive.TButton"}[kind]
        btn = ttk.Button(self.buttons, text=text, command=command, style=style)
        if width:
            btn.configure(width=width)
        pad = (px(8), 0) if side == "right" else (0, px(8))
        btn.pack(side=side, padx=pad)
        if default:
            self._default_btn = btn
        self._buttons.append(btn)
        return btn

    def _on_return(self, event=None):
        if _focused_is_text(self):
            return None
        if self._default_btn is not None and str(self._default_btn.cget("state")) != "disabled":
            self._default_btn.invoke()
            return "break"
        return None

    def show(self):
        """Modal: block until closed and return `self.result`."""
        center_over(self, self._parent)
        self.deiconify()
        self.lift()
        if self._modal:
            self.wait_visibility()
            self.grab_set()
        try:
            has_focus = self.focus_get() is not None
        except (KeyError, tk.TclError):
            has_focus = False
        if self._default_btn is not None and not has_focus:
            self._default_btn.focus_set()
        self.wait_window(self)
        return self.result

    def present(self):
        """Non-modal: show (or raise) and return immediately."""
        if not self.winfo_viewable():
            center_over(self, self._parent)
            self.deiconify()
        self.lift()
        self.focus_set()

    def close(self, result=None):
        self.result = result
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def cancel(self):
        self.close(None)


# =============================================================================
# Message dialogs
# =============================================================================

_KIND_ROLE = {"info": "accent_text", "question": "accent_text", "warning": "warning", "error": "error", "success": "success"}


class MessageDialog(Dialog):
    """Icon + heading + message (+ optional selectable detail) + buttons.

    `buttons` is a list of (label, value, kind); `default` is the value whose
    button Enter triggers; Escape returns `cancel_value`."""

    def __init__(self, parent, title, message, kind="info", buttons=(("OK", "ok", "accent"),),
                 default="ok", detail=None, cancel_value=None, wrap=440):
        super().__init__(parent, title)
        th = current()
        self._cancel_value = cancel_value
        row = ttk.Frame(self.body, style="Dialog.TFrame")
        row.pack(fill="both", expand=True)
        icon = ttk.Label(row, image=th.icon(kind if kind in _KIND_ROLE else "info", _KIND_ROLE.get(kind, "accent_text"), 32),
                         style="Dialog.TLabel")
        icon.pack(side="left", anchor="n", padx=(0, px(16)))
        col = ttk.Frame(row, style="Dialog.TFrame")
        col.pack(side="left", fill="both", expand=True)
        ttk.Label(col, text=title, style="DialogHeading.TLabel", wraplength=px(wrap), justify="left").pack(anchor="w")
        if message:
            ttk.Label(col, text=message, style="Dialog.TLabel", wraplength=px(wrap), justify="left").pack(anchor="w", pady=(px(6), 0))
        if detail:
            box = tk.Text(col, height=min(10, max(3, detail.count("\n") + 2)), width=56)
            th.style_preview(box)
            box.configure(wrap="word")
            box.insert("1.0", detail)
            box.configure(state="disabled")
            box.pack(fill="both", expand=True, pady=(px(10), 0))
        for label, value, bkind in reversed(list(buttons)):
            self.add_button(label, lambda v=value: self.close(v), kind=bkind, default=(value == default))

    def cancel(self):
        self.close(self._cancel_value)


def show_info(parent, title, message="", detail=None):
    return MessageDialog(parent, title, message, "info", detail=detail).show()


def show_error(parent, title, message="", detail=None):
    return MessageDialog(parent, title, message, "error", detail=detail).show()


def show_warning(parent, title, message="", detail=None):
    return MessageDialog(parent, title, message, "warning", detail=detail).show()


def ask_yes_no(parent, title, message="", yes="Yes", no="Cancel", destructive=False):
    kind = "destructive" if destructive else "accent"
    result = MessageDialog(
        parent, title, message, "question",
        buttons=((no, False, "normal"), (yes, True, kind)), default=False if destructive else True,
        cancel_value=False,
    ).show()
    return bool(result)


def ask_save_changes(parent, name):
    """'save', 'discard' or 'cancel' — the GNOME-style three-way close prompt."""
    result = MessageDialog(
        parent, "Save changes?",
        f"“{name}” has unsaved changes. If you don’t save, they will be lost.",
        "question",
        buttons=(("Cancel", "cancel", "normal"), ("Discard", "discard", "destructive"), ("Save", "save", "accent")),
        default="save", cancel_value="cancel",
    ).show()
    return result or "cancel"


class PromptDialog(Dialog):
    def __init__(self, parent, title, prompt, initial="", ok="OK", validate=None, mono=False):
        super().__init__(parent, title)
        th = current()
        self._validate = validate
        ttk.Label(self.body, text=prompt, style="Dialog.TLabel", wraplength=px(420), justify="left").pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        self.entry = ttk.Entry(self.body, textvariable=self.var, width=40, font=th.fonts.mono.name if mono else th.fonts.ui.name)
        self.entry.pack(fill="x", pady=(px(8), 0))
        self.error = ttk.Label(self.body, text="", style="DialogError.TLabel")
        self.error.pack(anchor="w", pady=(px(4), 0))
        self.add_button(ok, self._ok, kind="accent", default=True)
        self.add_button("Cancel", self.cancel)
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _ok(self):
        value = self.var.get()
        if self._validate is not None:
            problem = self._validate(value)
            if problem:
                self.error.configure(text=problem)
                self.entry.state(["invalid"])
                return
        self.close(value)


def ask_string(parent, title, prompt, initial="", ok="OK", validate=None, mono=False):
    return PromptDialog(parent, title, prompt, initial, ok, validate, mono).show()


# =============================================================================
# Tooltip
# =============================================================================

class Tooltip:
    """Delayed hover tooltip. `text` may be a string or a callable returning one."""

    def __init__(self, widget, text, delay=550):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text or self._tip is not None:
            return
        th = current()
        pal = th.pal
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tip.configure(background=pal.tooltip_bg)
        label = tk.Label(tip, text=text, background=pal.tooltip_bg, foreground=pal.tooltip_fg,
                         font=th.fonts.small.name, padx=px(10), pady=px(6), justify="left")
        label.pack()
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = self.widget.winfo_rootx() + (self.widget.winfo_width() - w) // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(6)
        sw, sh = self.widget.winfo_screenwidth(), self.widget.winfo_screenheight()
        x = max(px(4), min(x, sw - w - px(4)))
        if y + h > sh - px(4):
            y = self.widget.winfo_rooty() - h - px(6)
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


# =============================================================================
# Entry placeholder (overlay label; never touches the entry's variable)
# =============================================================================

class Placeholder:
    def __init__(self, entry, text, var=None, bg=None):
        self.entry = entry
        self.var = var
        th = current()
        pal = th.pal
        self.label = tk.Label(entry, text=text, background=bg or pal.view, foreground=pal.fg_faint,
                              font=th.fonts.ui.name, cursor="xterm", anchor="w")
        self.label.bind("<Button-1>", lambda e: (entry.focus_set(), "break")[1])
        entry.bind("<FocusIn>", self.refresh, add="+")
        entry.bind("<FocusOut>", self.refresh, add="+")
        entry.bind("<KeyRelease>", self.refresh, add="+")
        if var is not None:
            var.trace_add("write", lambda *a: self.refresh())
        if th is not None:
            th.add_listener(self._retheme)
        self.refresh()

    def _retheme(self, pal):
        try:
            self.label.configure(background=pal.view, foreground=pal.fg_faint)
        except tk.TclError:
            pass

    def refresh(self, event=None):
        try:
            empty = not (self.var.get() if self.var is not None else self.entry.get())
            focused = self.entry.focus_get() is self.entry
        except (tk.TclError, KeyError):
            return
        if empty and not focused:
            self.label.place(x=px(9), rely=0.5, anchor="w")
        else:
            self.label.place_forget()


# =============================================================================
# Auto-hiding scrollbar
# =============================================================================

class AutoScrollbar(ttk.Scrollbar):
    """A ttk scrollbar that removes itself while everything already fits,
    and comes back (with its original grid/pack options) when it doesn't."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._manager = None
        self._info = None
        self._hidden = False

    def set(self, first, last):
        first_f, last_f = float(first), float(last)
        if first_f <= 0.0 and last_f >= 1.0:
            self._hide()
        else:
            self._show()
        super().set(first, last)

    def _remember(self):
        if self._manager is None:
            manager = self.winfo_manager()
            if manager == "grid":
                self._manager, self._info = "grid", self.grid_info()
            elif manager == "pack":
                self._manager, self._info = "pack", self.pack_info()

    def _hide(self):
        if self._hidden:
            return
        self._remember()
        if self._manager == "grid":
            self.grid_remove()
        elif self._manager == "pack":
            self.pack_forget()
        else:
            return
        self._hidden = True

    def _show(self):
        if not self._hidden:
            return
        if self._manager == "grid":
            self.grid()
        elif self._manager == "pack":
            info = dict(self._info)
            info.pop("in", None)
            self.pack(**info)
        self._hidden = False


# =============================================================================
# Empty state panel
# =============================================================================

class EmptyState(ttk.Frame):
    """Centred icon + title + hint (+ optional accent action), for panes with nothing to show yet."""

    def __init__(self, parent, icon, title, hint="", action=None, action_command=None, style_bg="view"):
        frame_style = {"view": "View.TFrame", "sidebar": "Sidebar.TFrame", "window": "TFrame"}[style_bg]
        super().__init__(parent, style=frame_style)
        th = current()
        heading = {"view": "ViewHeading.TLabel", "sidebar": "Sidebar.TLabel", "window": "Heading.TLabel"}[style_bg]
        muted = {"view": "ViewMuted.TLabel", "sidebar": "SidebarMuted.TLabel", "window": "Muted.TLabel"}[style_bg]
        inner = ttk.Frame(self, style=frame_style)
        inner.place(relx=0.5, rely=0.45, anchor="center")
        self._icon_name = icon
        self._icon = ttk.Label(inner, image=th.icon(icon, "fg_faint", 40), style=heading)
        self._icon.pack(pady=(0, px(10)))
        ttk.Label(inner, text=title, style=heading, justify="center").pack()
        if hint:
            ttk.Label(inner, text=hint, style=muted, justify="center", wraplength=px(300)).pack(pady=(px(4), 0))
        if action and action_command:
            ttk.Button(inner, text=action, command=action_command, style="Accent.TButton").pack(pady=(px(14), 0))
        th.add_listener(self._retheme)
        self.bind("<Destroy>", lambda e: th.remove_listener(self._retheme), add="+")

    def _retheme(self, pal):
        try:
            self._icon.configure(image=current().icon(self._icon_name, "fg_faint", 40))
        except tk.TclError:
            pass


# =============================================================================
# Inline find / replace bar
# =============================================================================

class FindBar(ttk.Frame):
    """GNOME-Text-Editor-style search bar that lives above the editor instead
    of in a floating window. The owner provides find_var / replace_var /
    match_case_var and the find_next / find_previous / replace_current /
    replace_all / refresh_find_highlights methods."""

    def __init__(self, parent, owner):
        super().__init__(parent, style="View.TFrame", padding=(px(8), px(6), px(8), px(6)))
        self.owner = owner
        th = current()
        self._replace_visible = False
        self.shown = False

        row1 = ttk.Frame(self, style="View.TFrame")
        row1.pack(fill="x")
        self._search_icon = ttk.Label(row1, image=th.icon("search", "fg_muted"), style="View.TLabel")
        self._search_icon.pack(side="left", padx=(px(2), px(6)))
        self.find_entry = ttk.Entry(row1, textvariable=owner.find_var, width=30, font=th.fonts.mono.name, style="View.TEntry")
        self.find_entry.pack(side="left")
        Placeholder(self.find_entry, "Find", owner.find_var)
        self.count_label = ttk.Label(row1, text="", style="ViewMuted.TLabel", width=12, anchor="w")
        self.count_label.pack(side="left", padx=(px(10), px(4)))
        self.prev_btn = ttk.Button(row1, image=th.icon("arrow_up"), style="View.Icon.Toolbutton", command=owner.find_previous)
        self.prev_btn.pack(side="left")
        Tooltip(self.prev_btn, "Previous match (Shift+Enter)")
        self.next_btn = ttk.Button(row1, image=th.icon("arrow_down"), style="View.Icon.Toolbutton", command=owner.find_next)
        self.next_btn.pack(side="left")
        Tooltip(self.next_btn, "Next match (Enter)")
        self.case_btn = ttk.Checkbutton(row1, text="Aa", variable=owner.match_case_var, style="View.Tool.TCheckbutton",
                                        command=owner.refresh_find_highlights)
        self.case_btn.pack(side="left", padx=(px(6), 0))
        Tooltip(self.case_btn, "Match case")
        self.toggle_btn = ttk.Button(row1, image=th.icon("replace"), style="View.Icon.Toolbutton", command=self.toggle_replace)
        self.toggle_btn.pack(side="left", padx=(px(6), 0))
        Tooltip(self.toggle_btn, "Show replace (Ctrl+H)")
        self.close_btn = ttk.Button(row1, image=th.icon("close"), style="View.Icon.Toolbutton", command=self.hide)
        self.close_btn.pack(side="right")
        Tooltip(self.close_btn, "Close (Esc)")

        self.row2 = ttk.Frame(self, style="View.TFrame")
        self._replace_icon = ttk.Label(self.row2, image=th.icon("replace", "fg_muted"), style="View.TLabel")
        self._replace_icon.pack(side="left", padx=(px(2), px(6)))
        self.replace_entry = ttk.Entry(self.row2, textvariable=owner.replace_var, width=30, font=th.fonts.mono.name, style="View.TEntry")
        self.replace_entry.pack(side="left")
        Placeholder(self.replace_entry, "Replace with", owner.replace_var)
        ttk.Button(self.row2, text="Replace", command=owner.replace_current, style="View.Toolbutton").pack(side="left", padx=(px(10), 0))
        ttk.Button(self.row2, text="Replace all", command=owner.replace_all, style="View.Toolbutton").pack(side="left", padx=(px(4), 0))

        for entry in (self.find_entry, self.replace_entry):
            entry.bind("<Escape>", lambda e: (self.hide(), "break")[1])
            entry.bind("<Shift-Return>", lambda e: (owner.find_previous(), "break")[1])
        self.find_entry.bind("<Return>", lambda e: (owner.find_next(), "break")[1])
        self.find_entry.bind("<KP_Enter>", lambda e: (owner.find_next(), "break")[1])
        self.replace_entry.bind("<Return>", lambda e: (owner.replace_current(), "break")[1])
        self.find_entry.bind("<KeyRelease>", self._on_typed)
        owner.match_case_var.trace_add("write", lambda *a: owner.refresh_find_highlights())
        th.add_listener(self._retheme)
        self.bind("<Destroy>", lambda e: th.remove_listener(self._retheme), add="+")

    def _retheme(self, pal):
        th = current()
        try:
            self._search_icon.configure(image=th.icon("search", "fg_muted"))
            self._replace_icon.configure(image=th.icon("replace", "fg_muted"))
            self.prev_btn.configure(image=th.icon("arrow_up"))
            self.next_btn.configure(image=th.icon("arrow_down"))
            self.toggle_btn.configure(image=th.icon("replace"))
            self.close_btn.configure(image=th.icon("close"))
        except tk.TclError:
            pass

    def _on_typed(self, event):
        if event.keysym in ("Return", "KP_Enter", "Escape", "Up", "Down", "Tab"):
            return
        self.owner.refresh_find_highlights(reposition=True)

    def set_count(self, current_idx, total):
        if total == 0:
            self.count_label.configure(text="No matches" if self.owner.find_var.get() else "")
        elif current_idx is None:
            self.count_label.configure(text=f"{total} match{'es' if total != 1 else ''}")
        else:
            self.count_label.configure(text=f"{current_idx} of {total}")

    def show(self, replace=False, seed=None):
        if not self.shown:
            self.pack(side="top", fill="x", before=self.owner.editor_area)
            self.shown = True
        if replace and not self._replace_visible:
            self.toggle_replace()
        if seed:
            self.owner.find_var.set(seed)
        self.find_entry.focus_set()
        self.find_entry.select_range(0, "end")
        self.find_entry.icursor("end")
        self.owner.refresh_find_highlights()

    def toggle_replace(self):
        self._replace_visible = not self._replace_visible
        if self._replace_visible:
            self.row2.pack(fill="x", pady=(px(6), 0))
            self.replace_entry.focus_set()
        else:
            self.row2.pack_forget()
            self.find_entry.focus_set()

    def hide(self):
        if self.shown:
            self.pack_forget()
            self.shown = False
        self.owner.clear_find_highlights()
        self.set_count(None, 0)
        self.owner.editor.focus_set()

    def visible(self):
        return self.shown


# =============================================================================
# Command palette
# =============================================================================

def fuzzy_score(query, text):
    """Subsequence match score; None when `query` isn't a subsequence of `text`.
    Higher is better: prefix and word-start hits score most, contiguity next."""
    q = query.lower()
    t = text.lower()
    if not q:
        return 0
    pos = t.find(q)
    if pos == 0:
        return 1000 - len(t)
    if pos > 0:
        bonus = 60 if not t[pos - 1].isalnum() else 0
        return 700 + bonus - pos - len(t) // 4
    score = 0
    ti = 0
    prev = -2
    for ch in q:
        j = t.find(ch, ti)
        if j < 0:
            return None
        if j == 0 or not t[j - 1].isalnum():
            score += 30
        if j == prev + 1:
            score += 12
        score -= (j - ti)
        prev = j
        ti = j + 1
    return 300 + score - len(t) // 6


class CommandPalette:
    """Ctrl+Shift+P style quick launcher: type to filter commands, Enter runs."""

    def __init__(self, root, provider):
        self.root = root
        self.provider = provider
        self.win = None
        self.items = []
        self.visible_items = []

    def open(self):
        if self.win is not None:
            self.close()
        th = current()
        pal = th.pal
        self.items = list(self.provider())
        self.win = tk.Toplevel(self.root)
        self.win.wm_overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.win.configure(background=pal.border)
        inner = ttk.Frame(self.win, style="Popover.TFrame", padding=px(8))
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.var = tk.StringVar()
        row = ttk.Frame(inner, style="Popover.TFrame")
        row.pack(fill="x")
        ttk.Label(row, image=th.icon("terminal", "fg_muted"), style="Popover.TLabel").pack(side="left", padx=(px(4), px(8)))
        self.entry = ttk.Entry(row, textvariable=self.var, font=th.fonts.ui.name, style="Popover.TEntry")
        self.entry.pack(side="left", fill="x", expand=True)
        Placeholder(self.entry, "Type a command or a table name…", self.var, bg=pal.popover)
        self.listbox = ttk.Treeview(inner, columns=("title", "hint"), show="", height=10, selectmode="browse",
                                    style="Popover.Treeview", takefocus=0)
        self.listbox.column("title", width=px(380), stretch=True, anchor="w")
        self.listbox.column("hint", width=px(190), stretch=False, anchor="e")
        self.listbox.tag_configure("hint", foreground=pal.fg_muted)
        self.listbox.pack(fill="both", expand=True, pady=(px(8), 0))
        self.hint = ttk.Label(inner, text="↑↓ to navigate · Enter to run · Esc to close", style="Popover.TLabel")
        self.hint.configure(foreground=pal.fg_faint, font=th.fonts.small.name)
        self.hint.pack(anchor="w", pady=(px(6), 0))

        width = px(620)
        self.win.update_idletasks()
        rx = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        ry = self.root.winfo_rooty() + px(70)
        self.win.geometry(f"{width}x{self.win.winfo_reqheight()}+{max(0, rx)}+{max(0, ry)}")

        self.var.trace_add("write", lambda *a: self._refilter())
        self.entry.bind("<Escape>", lambda e: self.close())
        self.entry.bind("<Return>", lambda e: self._run())
        self.entry.bind("<KP_Enter>", lambda e: self._run())
        self.entry.bind("<Down>", lambda e: (self._move(1), "break")[1])
        self.entry.bind("<Up>", lambda e: (self._move(-1), "break")[1])
        self.entry.bind("<Next>", lambda e: (self._move(8), "break")[1])
        self.entry.bind("<Prior>", lambda e: (self._move(-8), "break")[1])
        self.listbox.bind("<ButtonRelease-1>", lambda e: self._run())
        self.listbox.bind("<Return>", lambda e: self._run())
        self.listbox.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", self._on_focus_out)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._refilter()
        self.entry.focus_force()

    def _on_root_configure(self, event=None):
        if self.win is not None:
            self.close()

    def _on_focus_out(self, event=None):
        if self.win is None:
            return
        self.win.after(120, self._check_focus)

    def _check_focus(self):
        if self.win is None:
            return
        try:
            focus = self.win.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None or focus.winfo_toplevel() is not self.win:
            self.close()

    def _refilter(self):
        query = self.var.get().strip()
        scored = []
        for item in self.items:
            title = item[0]
            keywords = item[3] if len(item) > 3 else ""
            s = fuzzy_score(query, title)
            if s is None and keywords:
                s = fuzzy_score(query, keywords)
                s = s - 50 if s is not None else None
            if s is not None:
                scored.append((s, title.lower(), item))
        if query:
            scored.sort(key=lambda x: (-x[0], x[1]))
        self.visible_items = [it for _, _, it in scored[:60]]
        self.listbox.delete(*self.listbox.get_children())
        for title, hint, _cb, *_ in self.visible_items:
            self.listbox.insert("", "end", values=(f"  {title}", f"{hint}  "))
        children = self.listbox.get_children()
        if children:
            self.listbox.selection_set(children[0])
            self.listbox.see(children[0])

    def _selected_index(self):
        children = self.listbox.get_children()
        sel = self.listbox.selection()
        if not sel or sel[0] not in children:
            return None, children
        return children.index(sel[0]), children

    def _move(self, delta):
        idx, children = self._selected_index()
        if not children:
            return
        idx = (idx if idx is not None else 0) + delta
        idx = max(0, min(len(children) - 1, idx))
        self.listbox.selection_set(children[idx])
        self.listbox.see(children[idx])

    def _run(self):
        idx, _children = self._selected_index()
        if idx is None or idx >= len(self.visible_items):
            return
        item = self.visible_items[idx]
        self.close()
        item[2]()

    def close(self):
        if self.win is not None:
            try:
                self.root.unbind("<Configure>")
            except tk.TclError:
                pass
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


# =============================================================================
# Status bar
# =============================================================================

class StatusBar(ttk.Frame):
    """Bottom bar: message with state icon on the left; caret position,
    timing and the connection badge on the right."""

    def __init__(self, parent, on_connection_click=None):
        super().__init__(parent, style="Status.TFrame")
        th = current()
        ttk.Separator(self, style="Strong.TSeparator").pack(side="top", fill="x")
        inner = ttk.Frame(self, style="Status.TFrame", padding=(px(10), px(3), px(10), px(3)))
        inner.pack(fill="x")
        self._kind = "info"
        self.icon = ttk.Label(inner, image=th.icon("info", "fg_muted", 14), style="Status.TLabel")
        self.icon.pack(side="left", padx=(0, px(6)))
        self.busy = ttk.Progressbar(inner, mode="indeterminate", length=px(90), style="Status.Horizontal.TProgressbar")
        self.message = ttk.Label(inner, text="", style="Status.TLabel", anchor="w")
        self.message.pack(side="left", fill="x", expand=True)

        self.conn_label = ttk.Label(inner, text="Not connected", style="StatusMuted.TLabel", cursor="hand2")
        self.conn_label.pack(side="right")
        self.conn_dot = ttk.Label(inner, image=th.icon("dot", "fg_faint", 12), style="Status.TLabel", cursor="hand2")
        self.conn_dot.pack(side="right", padx=(0, px(4)))
        if on_connection_click:
            for w in (self.conn_label, self.conn_dot):
                w.bind("<Button-1>", lambda e: on_connection_click())
            Tooltip(self.conn_label, "Connection — click to change (Ctrl+K)")
        self._sep(inner)
        self.timing = ttk.Label(inner, text="", style="StatusMuted.TLabel", width=11, anchor="e")
        self.timing.pack(side="right")
        self._sep(inner)
        self.position = ttk.Label(inner, text="Ln 1, Col 1", style="StatusMuted.TLabel", width=14, anchor="e")
        self.position.pack(side="right")
        self._conn_state = "disconnected"
        th.add_listener(self._retheme)

    def _sep(self, parent):
        ttk.Separator(parent, orient="vertical").pack(side="right", fill="y", padx=px(10), pady=px(2))

    def _retheme(self, pal):
        self._apply_icon()
        self.set_connection(self.conn_label.cget("text"), self._conn_state)

    def _apply_icon(self):
        th = current()
        name, role = {
            "info": ("info", "fg_muted"), "ok": ("success", "success"), "error": ("error", "error"),
            "warning": ("warning", "warning"), "busy": ("history", "accent_text"),
        }.get(self._kind, ("info", "fg_muted"))
        self.icon.configure(image=th.icon(name, role, 14))
        style = {"error": "StatusError.TLabel", "ok": "StatusOk.TLabel"}.get(self._kind, "Status.TLabel")
        self.message.configure(style=style)

    def set_message(self, text, kind="info"):
        self._kind = kind
        self.message.configure(text=text)
        self._apply_icon()

    def set_busy(self, busy):
        if busy:
            if not self.busy.winfo_ismapped():
                self.busy.pack(side="left", padx=(0, px(10)), before=self.message)
            self.busy.start(14)
        else:
            self.busy.stop()
            if self.busy.winfo_ismapped():
                self.busy.pack_forget()

    def set_position(self, line, col):
        self.position.configure(text=f"Ln {line}, Col {col}")

    def set_timing(self, text):
        self.timing.configure(text=text or "")

    def set_connection(self, text, state="disconnected"):
        th = current()
        self._conn_state = state
        role = {"connected": "success_bg", "busy": "warning_bg"}.get(state, "fg_faint")
        self.conn_dot.configure(image=th.icon("dot", role, 12))
        self.conn_label.configure(text=text, style="Status.TLabel" if state == "connected" else "StatusMuted.TLabel")


# =============================================================================
# File dialogs (desktop-native where possible)
# =============================================================================

def _zenity_available():
    return sys.platform.startswith("linux") and shutil.which("zenity") is not None and not os.environ.get("NOTEPADB_TK_DIALOGS")


def _zenity_filters(filetypes):
    args = []
    for label, patterns in filetypes or ():
        pats = patterns if isinstance(patterns, (list, tuple)) else str(patterns).split()
        pats = ["*" if p in ("*.*", "*") else p for p in pats]
        args.append(f"--file-filter={label} | {' '.join(pats)}")
    return args


def _run_zenity(parent, args):
    try:
        parent.update()
    except tk.TclError:
        pass
    try:
        proc = subprocess.run(["zenity"] + args, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    if proc.returncode == 0:
        return proc.stdout.strip() or None, True
    if proc.returncode == 1:
        return None, True  # user cancelled
    return None, False  # zenity itself failed -> fall back


def pick_open_file(parent, title, filetypes, initialdir=None):
    if _zenity_available():
        args = ["--file-selection", f"--title={title}"] + _zenity_filters(filetypes)
        if initialdir:
            args.append(f"--filename={os.path.join(initialdir, '')}")
        path, handled = _run_zenity(parent, args)
        if handled:
            return path
    return filedialog.askopenfilename(parent=parent, title=title, filetypes=filetypes, initialdir=initialdir) or None


def pick_save_file(parent, title, filetypes, initialfile=None, defaultextension=None, initialdir=None):
    if _zenity_available():
        args = ["--file-selection", "--save", "--confirm-overwrite", f"--title={title}"] + _zenity_filters(filetypes)
        start = os.path.join(initialdir or "", initialfile or "")
        if start:
            args.append(f"--filename={start}")
        path, handled = _run_zenity(parent, args)
        if handled:
            if path and defaultextension and not os.path.splitext(path)[1]:
                path += defaultextension
            return path
    return filedialog.asksaveasfilename(
        parent=parent, title=title, filetypes=filetypes, initialfile=initialfile,
        defaultextension=defaultextension, initialdir=initialdir,
    ) or None
