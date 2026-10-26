# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Dror Maor
"""
notepaDB visual system.

Everything that decides how the app *looks* lives here, so app.py only has
to say "give me the run icon" or "style this text widget":

* two palettes (light / dark) derived from GNOME's Adwaita design tokens,
  so the app feels native on a Linux desktop and still clean elsewhere;
* DPI-aware metrics (`px()`) and a font stack that follows the desktop's
  own UI / monospace font choice where it can;
* icons rasterized at runtime from tiny vector descriptions — no image
  files, any size, any theme colour, crisp on HiDPI;
* a ttk theme built from 9-slice images generated at startup: rounded
  buttons and fields, underline tabs with close buttons, chevron tree
  expanders, thin scrollbars.

Nothing here depends on the rest of the app, so it can be exercised on its
own (see the `__main__` demo at the bottom).
"""

import base64
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
import tkinter as tk
from tkinter import ttk, font as tkfont


# =============================================================================
# Palettes
# =============================================================================

class Palette(dict):
    """A colour palette with attribute access: `pal.accent` == `pal["accent"]`."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


LIGHT = Palette(
    name="light", dark=False,
    # surfaces
    window="#fafafb", view="#ffffff", sidebar="#f3f3f5", popover="#ffffff", dialog="#fafafb",
    # text
    fg="#2e2e32", fg_muted="#6c6c73", fg_faint="#a2a2a8",
    # lines & interaction surfaces
    border="#d4d4d8", border_soft="#e7e7eb", hover="#f0f0f3", pressed="#e2e2e6",
    # accent (Adwaita blue)
    accent="#3584e4", accent_hover="#1c71d8", accent_active="#1a5fb4", accent_fg="#ffffff",
    accent_text="#0461be", accent_soft="#e1ecfb",
    # editor
    selection="#bcd6f6", selection_inactive="#dfe9f6", current_line="#f5f7fa", caret="#1a1a1e",
    syn_kw="#0000cc", syn_str="#a31515", syn_comment="#008000", syn_num="#098658",
    syn_bracket="#cfdcf0", find_match="#fdf0b5", gutter_fg="#a2a2a8", gutter_current="#4a4a50",
    # semantic
    success="#007c3d", success_bg="#2ec27e", error="#c30000", error_bg="#e01b24",
    warning="#905400", warning_bg="#e5a50a",
    # results grid
    grid_header_bg="#f3f3f5", grid_header_fg="#4a4a50", grid_line="#e7e7eb", grid_zebra="#fafafb",
    grid_null="#a2a2a8", grid_sel="#e1ecfb", grid_sel_border="#3584e4",
    grid_index_bg="#f3f3f5", grid_index_fg="#8a8a90",
    # misc
    scroll_thumb="#c6c6cb", scroll_thumb_hover="#9e9ea4",
    tooltip_bg="#2e2e32", tooltip_fg="#ffffff",
)

DARK = Palette(
    name="dark", dark=True,
    window="#222226", view="#1d1d20", sidebar="#28282c", popover="#36363a", dialog="#222226",
    fg="#f0f0f3", fg_muted="#a4a4ac", fg_faint="#6e6e76",
    border="#45454b", border_soft="#313136", hover="#2e2e33", pressed="#3a3a40",
    accent="#3584e4", accent_hover="#4a90e8", accent_active="#62a0ea", accent_fg="#ffffff",
    accent_text="#81d0ff", accent_soft="#243b5a",
    selection="#264f78", selection_inactive="#2a3a4e", current_line="#25252a", caret="#ffffff",
    syn_kw="#569cd6", syn_str="#ce9178", syn_comment="#6a9955", syn_num="#b5cea8",
    syn_bracket="#3e4e63", find_match="#5a4a1e", gutter_fg="#6e6e76", gutter_current="#c8c8ce",
    success="#78e9ab", success_bg="#26a269", error="#ff938c", error_bg="#c01c28",
    warning="#ffc252", warning_bg="#cd9309",
    grid_header_bg="#28282c", grid_header_fg="#c8c8ce", grid_line="#333338", grid_zebra="#202024",
    grid_null="#6e6e76", grid_sel="#243b5a", grid_sel_border="#3584e4",
    grid_index_bg="#28282c", grid_index_fg="#8a8a92",
    scroll_thumb="#4a4a52", scroll_thumb_hover="#6e6e76",
    tooltip_bg="#3a3a3f", tooltip_fg="#f0f0f3",
)


def detect_system_dark():
    """Best-effort read of the desktop's light/dark preference. Returns
    True/False, or None when it can't be determined."""
    try:
        if sys.platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        if sys.platform == "darwin":
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            return out.returncode == 0 and "dark" in out.stdout.lower()
        # Linux / BSD: GNOME-style setting first, then a few common hints.
        if shutil.which("gsettings"):
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                value = out.stdout.strip().strip("'")
                if value == "prefer-dark":
                    return True
                if value == "prefer-light":
                    return False
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0 and "dark" in out.stdout.lower():
                return True
        gtk_theme = os.environ.get("GTK_THEME", "")
        if gtk_theme:
            return "dark" in gtk_theme.lower()
    except Exception:
        pass
    return None


# =============================================================================
# Fonts
# =============================================================================

_UI_CANDIDATES = {
    "win": ["Segoe UI Variable Text", "Segoe UI", "Tahoma", "Arial"],
    "mac": [".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue", "Helvetica"],
    "linux": ["Adwaita Sans", "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans", "Liberation Sans"],
}
_MONO_CANDIDATES = {
    "win": ["Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas", "Courier New"],
    "mac": ["SF Mono", "JetBrains Mono", "Menlo", "Monaco", "Courier New"],
    "linux": [
        "Adwaita Mono", "JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Fira Code",
        "Source Code Pro", "Hack", "Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono",
        "Ubuntu Mono", "Noto Mono",
    ],
}


def _platform_key():
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _gsettings_font(key):
    """Read e.g. 'Noto Sans 10' from GNOME settings -> ('Noto Sans', 10)."""
    if not shutil.which("gsettings"):
        return None, None
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", key],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode != 0:
            return None, None
        value = out.stdout.strip().strip("'")
        m = re.match(r"^(.*?)\s*(?:,\s*)?(\d+(?:\.\d+)?)?$", value)
        if not m:
            return None, None
        family = m.group(1).strip().rstrip(",")
        # Strip style words GNOME appends ("Cantarell Bold 11").
        family = re.sub(r"\s+(Bold|Italic|Regular|Medium|Light|Semi-?Bold|Oblique)$", "", family, flags=re.I)
        size = int(float(m.group(2))) if m.group(2) else None
        return family or None, size
    except Exception:
        return None, None


def _fc_match(name):
    """Resolve a fontconfig alias ('Sans', 'Monospace') to a real family name."""
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{family[0]}", name], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except Exception:
        return None


class Fonts:
    """Named Tk fonts for the whole app. Widgets reference the names, so a
    size change (editor zoom) updates everything at once."""

    def __init__(self, root):
        self.root = root
        available = set(tkfont.families(root))
        self.available = available
        key = _platform_key()

        ui_family = mono_family = None
        ui_size = mono_size = None
        if key == "linux":
            fam, size = _gsettings_font("font-name")
            if fam:
                ui_family, ui_size = self._resolve(fam, available), size
            fam, size = _gsettings_font("monospace-font-name")
            if fam:
                mono_family, mono_size = self._resolve(fam, available), size

        if not ui_family:
            ui_family = next((f for f in _UI_CANDIDATES[key] if f in available), None)
        if not mono_family:
            mono_family = next((f for f in _MONO_CANDIDATES[key] if f in available), None)

        if not ui_family:
            ui_family = tkfont.nametofont("TkDefaultFont").actual("family")
        if not mono_family:
            mono_family = tkfont.nametofont("TkFixedFont").actual("family")

        self.ui_family = ui_family
        self.mono_family = mono_family
        self.ui_size = ui_size or 10
        self.mono_size = mono_size or 11

        s = self.ui_size
        self.ui = self._named("NpUi", ui_family, s)
        self.ui_bold = self._named("NpUiBold", ui_family, s, weight="bold")
        self.small = self._named("NpSmall", ui_family, max(s - 1, 7))
        self.small_bold = self._named("NpSmallBold", ui_family, max(s - 1, 7), weight="bold")
        self.heading = self._named("NpHeading", ui_family, s + 1, weight="bold")
        self.title = self._named("NpTitle", ui_family, s + 5, weight="bold")
        self.display = self._named("NpDisplay", ui_family, s + 12, weight="bold")
        self.mono = self._named("NpMono", mono_family, self.mono_size)
        self.mono_bold = self._named("NpMonoBold", mono_family, self.mono_size, weight="bold")
        self.mono_small = self._named("NpMonoSmall", mono_family, max(self.mono_size - 1, 7))
        self.mono_small_bold = self._named("NpMonoSmallBold", mono_family, max(self.mono_size - 1, 7), weight="bold")

        # Tk's own named fonts drive menus, tooltips and any dialog we don't
        # draw ourselves, so bring them in line too.
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkIconFont"):
            tkfont.nametofont(name).configure(family=ui_family, size=s)
        tkfont.nametofont("TkHeadingFont").configure(family=ui_family, size=s, weight="bold")
        tkfont.nametofont("TkCaptionFont").configure(family=ui_family, size=s + 2, weight="bold")
        tkfont.nametofont("TkSmallCaptionFont").configure(family=ui_family, size=max(s - 1, 7))
        tkfont.nametofont("TkTooltipFont").configure(family=ui_family, size=max(s - 1, 7))
        tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=self.mono_size)

    @staticmethod
    def _resolve(family, available):
        if family in available:
            return family
        resolved = _fc_match(family)
        if resolved and resolved in available:
            return resolved
        return None

    def _named(self, name, family, size, weight="normal", slant="roman"):
        try:
            f = tkfont.nametofont(name)
            f.configure(family=family, size=size, weight=weight, slant=slant)
            return f
        except tk.TclError:
            return tkfont.Font(self.root, name=name, family=family, size=size, weight=weight, slant=slant)

    def set_editor_size(self, size):
        size = max(7, min(32, int(size)))
        self.mono.configure(size=size)
        self.mono_bold.configure(size=size)
        return size

    def editor_size(self):
        return self.mono.actual("size")

    def tuple(self, font, weight=None):
        """(family, size, weight) tuple, for libraries (tksheet) that want one."""
        return (font.actual("family"), font.actual("size"), weight or font.actual("weight"))


# =============================================================================
# Vector rasterizer (signed-distance shapes -> anti-aliased PNG)
# =============================================================================

def _circle(cx, cy, r):
    return lambda x, y: math.hypot(x - cx, y - cy) - r


def _ring(cx, cy, r, w):
    hw = w / 2.0
    return lambda x, y: abs(math.hypot(x - cx, y - cy) - r) - hw


def _arc(cx, cy, r, w, a0, a1):
    """Ring segment from angle a0 to a1 (degrees, counter-clockwise, y down), round caps."""
    hw = w / 2.0
    a0r, a1r = math.radians(a0), math.radians(a1)
    span = (a1r - a0r) % (2 * math.pi)
    p0 = (cx + r * math.cos(a0r), cy - r * math.sin(a0r))
    p1 = (cx + r * math.cos(a1r), cy - r * math.sin(a1r))

    def f(x, y):
        rel = (math.atan2(-(y - cy), x - cx) - a0r) % (2 * math.pi)
        if rel <= span:
            return abs(math.hypot(x - cx, y - cy) - r) - hw
        return min(math.hypot(x - p0[0], y - p0[1]), math.hypot(x - p1[0], y - p1[1])) - hw
    return f


def _segment(x0, y0, x1, y1, w):
    hw = w / 2.0
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy or 1e-9

    def f(x, y):
        t = ((x - x0) * dx + (y - y0) * dy) / l2
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        return math.hypot(x - (x0 + t * dx), y - (y0 + t * dy)) - hw
    return f


def _polyline(pts, w, closed=False):
    segs = [_segment(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], w) for i in range(len(pts) - 1)]
    if closed:
        segs.append(_segment(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1], w))
    return lambda x, y: min(s(x, y) for s in segs)


def _rrect(x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hx, hy = (x1 - x0) / 2.0 - r, (y1 - y0) / 2.0 - r

    def f(x, y):
        qx, qy = abs(x - cx) - hx, abs(y - cy) - hy
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
    return f


def _polygon(pts):
    n = len(pts)

    def f(x, y):
        d = float("inf")
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            ex, ey = xj - xi, yj - yi
            wx, wy = x - xi, y - yi
            t = (wx * ex + wy * ey) / (ex * ex + ey * ey or 1e-9)
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            bx, by = wx - ex * t, wy - ey * t
            dd = bx * bx + by * by
            if dd < d:
                d = dd
            if (yi > y) != (yj > y) and x < ex * (y - yi) / (ey or 1e-9) + xi:
                inside = not inside
            j = i
        return -math.sqrt(d) if inside else math.sqrt(d)
    return f


def _ellipse(cx, cy, rx, ry):
    """First-order distance estimate f/|grad f| for the implicit ellipse —
    accurate at the outline, which is all anti-aliasing needs."""
    def f(x, y):
        dx, dy = x - cx, y - cy
        k = math.hypot(dx / rx, dy / ry)
        if k < 1e-6:
            return -min(rx, ry)
        g = math.hypot(dx / (rx * rx), dy / (ry * ry))
        return (k - 1.0) * k / g if g > 1e-9 else (k - 1.0) * min(rx, ry)
    return f


def _ellipse_ring(cx, cy, rx, ry, w):
    e = _ellipse(cx, cy, rx, ry)
    hw = w / 2.0
    return lambda x, y: abs(e(x, y)) - hw


def _halfplane_below(y0):
    return lambda x, y: y0 - y


def _halfplane_above(y0):
    return lambda x, y: y - y0


def _union(*fs):
    return lambda x, y: min(f(x, y) for f in fs)


def _subtract(a, b):
    return lambda x, y: max(a(x, y), -b(x, y))


def _intersect(a, b):
    return lambda x, y: max(a(x, y), b(x, y))


def _nothing():
    return lambda x, y: 1e9


def _scaled(shape, k):
    """Shape defined on a 16-unit grid, rendered at k * 16 pixels."""
    return lambda x, y: shape(x / k, y / k) * k


def _coverage(shape, w, h, ss=2):
    out = bytearray(w * h)
    offs = [(i + 0.5) / ss for i in range(ss)]
    n = ss * ss
    i = 0
    for py in range(h):
        for px in range(w):
            acc = 0.0
            for oy in offs:
                for ox in offs:
                    c = 0.5 - shape(px + ox, py + oy)
                    acc += 1.0 if c >= 1 else (0.0 if c <= 0 else c)
            out[i] = int(acc / n * 255 + 0.5)
            i += 1
    return bytes(out)


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(w, h, rgba):
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


def _rgb(color):
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(color_a, color_b, t):
    """Linear blend of two hex colours; t=0 -> a, t=1 -> b."""
    a, b = _rgb(color_a), _rgb(color_b)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _composite(layers, w, h):
    """layers: [(mask, hex colour, alpha 0..255)] painted in order onto transparency."""
    acc = [0.0] * (w * h * 4)
    for mask, color, alpha in layers:
        r, g, b = _rgb(color)
        for i, a in enumerate(mask):
            if not a:
                continue
            sa = a * alpha / 65025.0
            j = i * 4
            da = acc[j + 3]
            na = sa + da * (1 - sa)
            if na <= 0:
                continue
            acc[j] = (r * sa + acc[j] * da * (1 - sa)) / na
            acc[j + 1] = (g * sa + acc[j + 1] * da * (1 - sa)) / na
            acc[j + 2] = (b * sa + acc[j + 2] * da * (1 - sa)) / na
            acc[j + 3] = na
    out = bytearray(w * h * 4)
    for j in range(0, w * h * 4, 4):
        out[j] = int(acc[j] + 0.5)
        out[j + 1] = int(acc[j + 1] + 0.5)
        out[j + 2] = int(acc[j + 2] + 0.5)
        out[j + 3] = int(acc[j + 3] * 255 + 0.5)
    return bytes(out)


def _photo(root, name, w, h, layers):
    rgba = _composite([(_coverage(s, w, h), c, a) for s, c, a in layers], w, h)
    return tk.PhotoImage(master=root, name=name, data=base64.b64encode(_png(w, h, rgba)).decode("ascii"))


# =============================================================================
# Icons (16-unit grid)
# =============================================================================

def _icon_play():
    return _polygon([(4.5, 2.5), (14, 8), (4.5, 13.5)])


def _icon_stop():
    return _rrect(3.5, 3.5, 12.5, 12.5, 2)


def _icon_plug():
    return _union(
        _rrect(4.5, 6, 11.5, 11, 1.4),
        _segment(6.3, 2.2, 6.3, 6.2, 1.7), _segment(9.7, 2.2, 9.7, 6.2, 1.7),
        _segment(8, 11, 8, 14.5, 1.7),
    )


def _icon_plug_off():
    return _union(_icon_plug(), _segment(2.5, 14, 13.5, 2.5, 1.5))


def _icon_refresh():
    return _union(_arc(8, 8.3, 5, 1.7, 35, 335), _polygon([(14.3, 2.3), (14.3, 7.6), (9.0, 7.6)]))


def _icon_plus():
    return _union(_segment(8, 3, 8, 13, 1.8), _segment(3, 8, 13, 8, 1.8))


def _icon_minus():
    return _segment(3, 8, 13, 8, 1.8)


def _icon_close():
    return _union(_segment(4.5, 4.5, 11.5, 11.5, 1.6), _segment(11.5, 4.5, 4.5, 11.5, 1.6))


def _icon_check():
    return _polyline([(3.2, 8.4), (6.6, 11.6), (12.8, 4.6)], 2.0)


def _icon_chev_right():
    return _polyline([(6, 3.5), (10.5, 8), (6, 12.5)], 1.7)


def _icon_chev_down():
    return _polyline([(3.5, 6), (8, 10.5), (12.5, 6)], 1.7)


def _icon_chev_up():
    return _polyline([(3.5, 10), (8, 5.5), (12.5, 10)], 1.7)


def _icon_arrow_up():
    return _union(_segment(8, 3, 8, 13.5, 1.7), _polyline([(3.8, 7.2), (8, 3), (12.2, 7.2)], 1.7))


def _icon_arrow_down():
    return _union(_segment(8, 2.5, 8, 13, 1.7), _polyline([(3.8, 8.8), (8, 13), (12.2, 8.8)], 1.7))


def _icon_search():
    return _union(_ring(6.8, 6.8, 4.3, 1.7), _segment(10.1, 10.1, 13.8, 13.8, 2.1))


def _icon_folder():
    return _union(_rrect(1.5, 4.5, 14.5, 13.5, 1.5), _rrect(1.5, 2.5, 7.5, 6, 1.2))


def _icon_save():
    return _subtract(
        _rrect(2, 2, 14, 14, 1.8),
        _union(_rrect(4.5, 3.6, 11.5, 6.6, 0.6), _rrect(5.2, 9.5, 10.8, 14, 0.6)),
    )


def _icon_format():
    return _union(
        _segment(3, 3.5, 13, 3.5, 1.8), _segment(3, 6.5, 9.5, 6.5, 1.8),
        _segment(3, 9.5, 13, 9.5, 1.8), _segment(3, 12.5, 8.5, 12.5, 1.8),
    )


def _icon_history():
    return _union(_ring(8, 8, 5.6, 1.6), _segment(8, 8, 8, 4.6, 1.6), _segment(8, 8, 10.6, 9.6, 1.6))


def _icon_export():
    return _union(
        _segment(8, 2.2, 8, 9.6, 1.8), _polyline([(4.8, 6.8), (8, 10), (11.2, 6.8)], 1.8),
        _polyline([(2.8, 10.3), (2.8, 13.5), (13.2, 13.5), (13.2, 10.3)], 1.6),
    )


def _icon_database():
    """Stroke-based cylinder: every stroke sits on the same outline so the
    rim doesn't flare out into a 'pot' at small sizes."""
    cx, rx, ry, w = 8.0, 5.5, 2.0, 1.4
    top_y, bot_y = 3.9, 12.1
    top = _ellipse_ring(cx, top_y, rx, ry, w)
    sides = _union(_segment(cx - rx, top_y, cx - rx, bot_y, w), _segment(cx + rx, top_y, cx + rx, bot_y, w))
    # Front edges of a cylinder bulge downwards (y grows downwards), so keep
    # the lower half of the mid and bottom ellipses.
    bottom = _intersect(_ellipse_ring(cx, bot_y, rx, ry, w), _halfplane_below(bot_y))
    mid = _intersect(_ellipse_ring(cx, 8.0, rx, ry, w), _halfplane_below(8.0))
    return _union(top, sides, bottom, mid)


def _icon_table():
    return _union(
        _subtract(_rrect(2, 2.5, 14, 13.5, 1.6), _rrect(3.5, 4, 12.5, 12, 0.5)),
        _segment(2, 7, 14, 7, 1.5), _segment(7.2, 7, 7.2, 13.5, 1.5),
    )


def _icon_column():
    return _union(
        _subtract(_rrect(5, 2, 11, 14, 1.6), _rrect(6.5, 3.5, 9.5, 12.5, 0.6)),
        _segment(5, 6, 11, 6, 1.5),
    )


def _icon_key():
    return _union(_ring(5.2, 10.8, 2.8, 1.6), _segment(7.3, 8.7, 13.3, 2.7, 1.8), _segment(11.2, 4.8, 13, 6.6, 1.6))


def _icon_dot():
    return _circle(8, 8, 3.6)


def _icon_info():
    return _subtract(_circle(8, 8, 7), _union(_circle(8, 4.9, 1.15), _rrect(6.95, 7, 9.05, 12.2, 0.9)))


def _icon_warning():
    body = _polygon([(8, 1.6), (15.2, 14.2), (0.8, 14.2)])
    return _subtract(body, _union(_rrect(7.05, 5.6, 8.95, 10.2, 0.9), _circle(8, 12.1, 1.1)))


def _icon_error():
    return _subtract(_circle(8, 8, 7), _union(_segment(5.3, 5.3, 10.7, 10.7, 1.6), _segment(10.7, 5.3, 5.3, 10.7, 1.6)))


def _icon_success():
    return _subtract(_circle(8, 8, 7), _polyline([(4.4, 8.3), (7, 10.8), (11.8, 5.6)], 1.8))


def _icon_question():
    glyph = _union(_arc(8, 6.2, 2.4, 1.5, -45, 200), _segment(8, 8.3, 8, 9.9, 1.5), _circle(8, 12.1, 1.05))
    return _subtract(_circle(8, 8, 7), glyph)


def _icon_terminal():
    return _union(_polyline([(3.2, 4.2), (7.3, 8), (3.2, 11.8)], 1.8), _segment(8.5, 12.2, 13.2, 12.2, 1.8))


def _icon_sun():
    rays = []
    for k in range(8):
        a = math.radians(k * 45)
        rays.append(_segment(8 + 5.2 * math.cos(a), 8 + 5.2 * math.sin(a), 8 + 7 * math.cos(a), 8 + 7 * math.sin(a), 1.5))
    return _union(_circle(8, 8, 3.1), *rays)


def _icon_moon():
    return _subtract(_circle(8, 8, 6.2), _circle(10.8, 5.6, 5.6))


def _icon_sidebar():
    return _union(_subtract(_rrect(1.8, 3, 14.2, 13, 1.6), _rrect(3.3, 4.5, 12.7, 11.5, 0.5)), _rrect(3.3, 4.5, 6.4, 11.5, 0.3))


def _icon_copy():
    return _union(
        _subtract(_rrect(5.5, 5.5, 14, 14, 1.4), _rrect(7, 7, 12.5, 12.5, 0.5)),
        _subtract(_rrect(2, 2, 10.5, 10.5, 1.4), _union(_rrect(3.5, 3.5, 9, 9, 0.5), _rrect(5.5, 5.5, 14, 14, 1.4))),
    )


def _icon_replace():
    return _union(
        _subtract(_rrect(2, 2, 8.5, 8.5, 1.2), _rrect(3.4, 3.4, 7.1, 7.1, 0.4)),
        _rrect(7.5, 7.5, 14, 14, 1.2),
        _polyline([(10.5, 2.5), (13.5, 2.5), (13.5, 5.5)], 1.4),
    )


def _icon_grip():
    return _union(*[_circle(8, y, 1.25) for y in (4, 8, 12)])


def _icon_tab():
    return _union(_subtract(_rrect(1.8, 3, 14.2, 13.5, 1.6), _rrect(3.3, 6.3, 12.7, 12, 0.4)), _rrect(3.3, 3, 8.5, 6.3, 0.2))


def _icon_logo():
    """The notepaDB mark: a database cylinder with three notepad rings on top (32-unit grid)."""
    def f32(shape):
        return lambda x, y: shape(x * 2, y * 2) / 2
    top = _ellipse_ring(16, 10.2, 10.5, 3.85, 2.4)
    sides = _union(_segment(5.5, 10.2, 5.5, 22.8, 2.4), _segment(26.5, 10.2, 26.5, 22.8, 2.4))
    bottom = _intersect(_ellipse_ring(16, 22.8, 10.5, 3.85, 2.4), _halfplane_below(22.8))
    mid = _intersect(_ellipse_ring(16, 16.5, 10.5, 3.85, 2.4), _halfplane_below(16.5))
    bars = _union(_rrect(9.6, 3, 12.2, 11.6, 1.3), _rrect(14.7, 2.2, 17.3, 11.6, 1.3), _rrect(19.8, 3, 22.4, 11.6, 1.3))
    return f32(_union(top, sides, bottom, mid, bars))


ICONS = {
    "play": _icon_play, "stop": _icon_stop, "plug": _icon_plug, "plug_off": _icon_plug_off,
    "refresh": _icon_refresh, "plus": _icon_plus, "minus": _icon_minus, "close": _icon_close,
    "check": _icon_check, "chev_right": _icon_chev_right, "chev_down": _icon_chev_down,
    "chev_up": _icon_chev_up, "arrow_up": _icon_arrow_up, "arrow_down": _icon_arrow_down,
    "search": _icon_search, "folder": _icon_folder, "save": _icon_save, "format": _icon_format,
    "history": _icon_history, "export": _icon_export, "database": _icon_database,
    "table": _icon_table, "column": _icon_column, "key": _icon_key, "dot": _icon_dot,
    "info": _icon_info, "warning": _icon_warning, "error": _icon_error, "success": _icon_success,
    "question": _icon_question, "terminal": _icon_terminal, "sun": _icon_sun, "moon": _icon_moon,
    "sidebar": _icon_sidebar, "copy": _icon_copy, "replace": _icon_replace, "grip": _icon_grip,
    "tab": _icon_tab, "logo": _icon_logo,
}


class IconSet:
    """Rasterizes ICONS on demand, cached by (name, size, colour)."""

    def __init__(self, root):
        self.root = root
        self._masks = {}
        self._images = {}

    def mask(self, name, size):
        key = (name, size)
        if key not in self._masks:
            shape = _scaled(ICONS[name](), size / 16.0)
            self._masks[key] = _coverage(shape, size, size, ss=2)
        return self._masks[key]

    def get(self, name, color, size, pad_right=0):
        """`pad_right` adds transparent columns after the glyph — handy where a
        widget (ttk.Treeview) butts the image straight against its text."""
        key = (name, size, color, pad_right)
        img = self._images.get(key)
        if img is None:
            mask = self.mask(name, size)
            r, g, b = _rgb(color)
            w = size + pad_right
            rgba = bytearray(w * size * 4)
            for i, a in enumerate(mask):
                if not a:
                    continue
                y, x = divmod(i, size)
                j = (y * w + x) * 4
                rgba[j] = r
                rgba[j + 1] = g
                rgba[j + 2] = b
                rgba[j + 3] = a
            img = tk.PhotoImage(master=self.root, data=base64.b64encode(_png(w, size, bytes(rgba))).decode("ascii"))
            self._images[key] = img
        return img

    def blank(self, size):
        key = ("__blank__", size, "")
        if key not in self._images:
            self._images[key] = tk.PhotoImage(master=self.root, width=size, height=size)
        return self._images[key]


# =============================================================================
# ttk theme
# =============================================================================

class _Images:
    """Generates and keeps alive the 9-slice images one ttk theme needs."""

    def __init__(self, root, prefix):
        self.root = root
        self.prefix = prefix
        self.keep = {}

    def rounded(self, name, size, radius, fill, border=None, bw=1.0, inset=0.5):
        layers = []
        if border:
            layers.append((_rrect(inset, inset, size - inset, size - inset, radius), border, 255))
            layers.append((_rrect(inset + bw, inset + bw, size - inset - bw, size - inset - bw, max(radius - bw, 0.5)), fill, 255))
        else:
            layers.append((_rrect(inset, inset, size - inset, size - inset, radius), fill, 255))
        return self._make(name, size, size, layers)

    def flat(self, name, size, fill, alpha=255):
        return self._make(name, size, size, [(_rrect(0, 0, size, size, 0), fill, alpha)] if fill else [])

    def tab(self, name, size, fill, line_color, line_h, bottom_rule=None):
        layers = []
        if fill:
            layers.append((_rrect(0, 0, size, size, 0), fill, 255))
        if bottom_rule:
            layers.append((_rrect(0, size - 1, size, size, 0), bottom_rule, 255))
        if line_color and line_h:
            layers.append((_rrect(0, size - line_h, size, size, 0), line_color, 255))
        return self._make(name, size, size, layers)

    def thumb(self, name, w, h, color, vertical=True, inset=2):
        if vertical:
            shape = _rrect(inset, 1, w - inset, h - 1, (w - 2 * inset) / 2.0)
        else:
            shape = _rrect(1, inset, w - 1, h - inset, (h - 2 * inset) / 2.0)
        return self._make(name, w, h, [(shape, color, 255)])

    def bar(self, name, w, h, color, radius):
        return self._make(name, w, h, [(_rrect(0, 0, w, h, radius), color, 255)])

    def _make(self, name, w, h, layers):
        full = f"{self.prefix}{name}"
        img = _photo(self.root, full, w, h, layers)
        self.keep[full] = img
        return full


def _theme_settings(pal, px, fonts, im, icons):
    """Build the ttk settings dict for one palette. `im` is an _Images store."""
    R = px(6)           # control corner radius
    S = px(28)          # 9-slice base size for stretchable control images
    B = px(10)          # slice border (must exceed the radius)
    tab_line = px(2)
    sb = px(10)         # scrollbar width
    isz = px(16)        # inline icon size

    # ---- control images ----
    btn = im.rounded("btn", S, R, pal.view if not pal.dark else pal.hover, pal.border)
    btn_hover = im.rounded("btn_hover", S, R, pal.hover if not pal.dark else pal.pressed, pal.border)
    btn_press = im.rounded("btn_press", S, R, pal.pressed, pal.border)
    btn_focus = im.rounded("btn_focus", S, R, pal.view if not pal.dark else pal.hover, pal.accent, bw=1.5)
    btn_disabled = im.rounded("btn_disabled", S, R, pal.window, pal.border_soft)
    acc = im.rounded("acc", S, R, pal.accent)
    acc_hover = im.rounded("acc_hover", S, R, pal.accent_hover)
    acc_press = im.rounded("acc_press", S, R, pal.accent_active)
    acc_focus = im.rounded("acc_focus", S, R, pal.accent, mix(pal.accent, pal.fg, 0.5), bw=1.5)
    acc_disabled = im.rounded("acc_disabled", S, R, mix(pal.accent, pal.window, 0.55))
    dstr = im.rounded("dstr", S, R, pal.error_bg)
    dstr_hover = im.rounded("dstr_hover", S, R, mix(pal.error_bg, "#000000", 0.12))
    dstr_disabled = im.rounded("dstr_disabled", S, R, mix(pal.error_bg, pal.window, 0.55))
    tool = im.flat("tool", S, None)
    tool_hover = im.rounded("tool_hover", S, R, pal.hover)
    tool_press = im.rounded("tool_press", S, R, pal.pressed)
    tool_sel = im.rounded("tool_sel", S, R, pal.pressed, pal.border)
    field = im.rounded("field", S, R, pal.view, pal.border)
    field_hover = im.rounded("field_hover", S, R, pal.view, mix(pal.border, pal.fg, 0.25))
    field_focus = im.rounded("field_focus", S, R, pal.view, pal.accent, bw=2.0)
    field_disabled = im.rounded("field_disabled", S, R, pal.window, pal.border_soft)
    field_invalid = im.rounded("field_invalid", S, R, pal.view, pal.error_bg, bw=1.5)
    tab = im.tab("tab", S, None, None, 0, bottom_rule=pal.border)
    tab_hover = im.tab("tab_hover", S, pal.hover, None, 0, bottom_rule=pal.border)
    tab_sel = im.tab("tab_sel", S, pal.view, pal.accent, tab_line)
    tab_sel_hover = im.tab("tab_sel_hover", S, pal.view, pal.accent_hover, tab_line)
    client = im.flat("client", S, pal.view)
    thumb_v = im.thumb("thumb_v", sb, S, pal.scroll_thumb, True, inset=px(3))
    thumb_v_hover = im.thumb("thumb_v_hover", sb, S, pal.scroll_thumb_hover, True, inset=px(2))
    thumb_h = im.thumb("thumb_h", S, sb, pal.scroll_thumb, False, inset=px(3))
    thumb_h_hover = im.thumb("thumb_h_hover", S, sb, pal.scroll_thumb_hover, False, inset=px(2))
    trough_v = im.flat("trough_v", sb, None)
    pbar = im.bar("pbar", S, px(6), pal.accent, px(3))
    ptrough = im.bar("ptrough", S, px(6), pal.border_soft if not pal.dark else pal.hover, px(3))

    cb = px(18)
    cb_r = px(4)
    cb_off = im.rounded("cb_off", cb, cb_r, pal.view, mix(pal.border, pal.fg, 0.3), bw=1.2, inset=1.5)
    cb_off_hover = im.rounded("cb_off_hover", cb, cb_r, pal.hover, mix(pal.border, pal.fg, 0.5), bw=1.2, inset=1.5)
    cb_on = im._make("cb_on", cb, cb, [
        (_rrect(1.5, 1.5, cb - 1.5, cb - 1.5, cb_r), pal.accent, 255),
        (_scaled(_polyline([(4.2, 8.4), (7, 11.2), (12.2, 5.2)], 2.0), cb / 16.0), pal.accent_fg, 255),
    ])
    cb_on_hover = im._make("cb_on_hover", cb, cb, [
        (_rrect(1.5, 1.5, cb - 1.5, cb - 1.5, cb_r), pal.accent_hover, 255),
        (_scaled(_polyline([(4.2, 8.4), (7, 11.2), (12.2, 5.2)], 2.0), cb / 16.0), pal.accent_fg, 255),
    ])
    cb_dis = im.rounded("cb_dis", cb, cb_r, pal.window, pal.border_soft, bw=1.2, inset=1.5)
    cb_on_dis = im._make("cb_on_dis", cb, cb, [
        (_rrect(1.5, 1.5, cb - 1.5, cb - 1.5, cb_r), mix(pal.accent, pal.window, 0.55), 255),
        (_scaled(_polyline([(4.2, 8.4), (7, 11.2), (12.2, 5.2)], 2.0), cb / 16.0), pal.accent_fg, 255),
    ])
    rb_off = im._make("rb_off", cb, cb, [(_circle(cb / 2, cb / 2, cb / 2 - 1.5), mix(pal.border, pal.fg, 0.3), 255),
                                           (_circle(cb / 2, cb / 2, cb / 2 - 2.7), pal.view, 255)])
    rb_off_hover = im._make("rb_off_hover", cb, cb, [(_circle(cb / 2, cb / 2, cb / 2 - 1.5), mix(pal.border, pal.fg, 0.5), 255),
                                                       (_circle(cb / 2, cb / 2, cb / 2 - 2.7), pal.hover, 255)])
    rb_on = im._make("rb_on", cb, cb, [(_circle(cb / 2, cb / 2, cb / 2 - 1.5), pal.accent, 255),
                                         (_circle(cb / 2, cb / 2, cb / 6.0), pal.accent_fg, 255)])
    rb_on_hover = im._make("rb_on_hover", cb, cb, [(_circle(cb / 2, cb / 2, cb / 2 - 1.5), pal.accent_hover, 255),
                                                     (_circle(cb / 2, cb / 2, cb / 6.0), pal.accent_fg, 255)])
    rb_dis = im._make("rb_dis", cb, cb, [(_circle(cb / 2, cb / 2, cb / 2 - 1.5), pal.border_soft, 255),
                                           (_circle(cb / 2, cb / 2, cb / 2 - 2.7), pal.window, 255)])

    chev_r = str(icons.get("chev_right", pal.fg_muted, isz))
    chev_d = str(icons.get("chev_down", pal.fg_muted, isz))
    chev_d_dim = str(icons.get("chev_down", pal.fg_faint, isz))
    chev_up = str(icons.get("chev_up", pal.fg_muted, isz))
    empty = str(icons.blank(isz))
    xs = px(14)
    close = str(icons.get("close", pal.fg_faint, xs))
    close_hot = str(icons.get("close", pal.fg, xs))
    close_pressed = str(icons.get("close", pal.error, xs))

    ui_font = fonts.ui.name
    pad_btn = (px(14), px(6))
    pad_field = (px(8), px(5))

    def button_layout(element):
        return [(element, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]

    settings = {
        ".": {
            "configure": {
                "background": pal.window, "foreground": pal.fg, "bordercolor": pal.border,
                "lightcolor": pal.window, "darkcolor": pal.window, "troughcolor": pal.window,
                "focuscolor": pal.window, "selectbackground": pal.accent, "selectforeground": pal.accent_fg,
                "fieldbackground": pal.view, "font": ui_font, "borderwidth": 1, "insertcolor": pal.fg,
                "insertwidth": px(2),
            },
            "map": {"foreground": [("disabled", pal.fg_faint)]},
        },
        # ---- frames / labels ----
        "TFrame": {"configure": {"background": pal.window}},
        "View.TFrame": {"configure": {"background": pal.view}},
        "Sidebar.TFrame": {"configure": {"background": pal.sidebar}},
        "Popover.TFrame": {"configure": {"background": pal.popover}},
        "Dialog.TFrame": {"configure": {"background": pal.dialog}},
        "Card.TFrame": {"configure": {"background": pal.view, "bordercolor": pal.border, "lightcolor": pal.border,
                                      "darkcolor": pal.border, "relief": "solid", "borderwidth": 1}},
        "SidebarCard.TFrame": {"configure": {"background": pal.sidebar, "bordercolor": pal.border, "lightcolor": pal.border,
                                             "darkcolor": pal.border, "relief": "solid", "borderwidth": 1}},
        "TLabel": {"configure": {"background": pal.window, "foreground": pal.fg}},
        "View.TLabel": {"configure": {"background": pal.view, "foreground": pal.fg}},
        "Sidebar.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.fg}},
        "Dialog.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.fg}},
        "Popover.TLabel": {"configure": {"background": pal.popover, "foreground": pal.fg}},
        "Muted.TLabel": {"configure": {"background": pal.window, "foreground": pal.fg_muted}},
        "ViewMuted.TLabel": {"configure": {"background": pal.view, "foreground": pal.fg_muted}},
        "SidebarMuted.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.fg_muted}},
        "DialogMuted.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.fg_muted}},
        "Faint.TLabel": {"configure": {"background": pal.window, "foreground": pal.fg_faint}},
        "ViewFaint.TLabel": {"configure": {"background": pal.view, "foreground": pal.fg_faint}},
        "Heading.TLabel": {"configure": {"background": pal.window, "foreground": pal.fg, "font": fonts.heading.name}},
        "ViewHeading.TLabel": {"configure": {"background": pal.view, "foreground": pal.fg, "font": fonts.heading.name}},
        "SidebarHeading.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.fg_muted, "font": fonts.small_bold.name}},
        "DialogHeading.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.fg, "font": fonts.heading.name}},
        "Title.TLabel": {"configure": {"background": pal.window, "foreground": pal.fg, "font": fonts.title.name}},
        "DialogTitle.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.fg, "font": fonts.title.name}},
        "Display.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.fg, "font": fonts.display.name}},
        "Link.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.accent_text}},
        "Error.TLabel": {"configure": {"background": pal.window, "foreground": pal.error}},
        "DialogError.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.error}},
        "DialogSuccess.TLabel": {"configure": {"background": pal.dialog, "foreground": pal.success}},
        "Status.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.fg, "font": fonts.small.name}},
        "StatusMuted.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.fg_muted, "font": fonts.small.name}},
        "StatusError.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.error, "font": fonts.small.name}},
        "StatusOk.TLabel": {"configure": {"background": pal.sidebar, "foreground": pal.success, "font": fonts.small.name}},
        "Small.TLabel": {"configure": {"background": pal.window, "foreground": pal.fg_muted, "font": fonts.small.name}},
        "ViewSmall.TLabel": {"configure": {"background": pal.view, "foreground": pal.fg_muted, "font": fonts.small.name}},
        "Toolbar.TFrame": {"configure": {"background": pal.window}},
        "Status.TFrame": {"configure": {"background": pal.sidebar}},
        "TSeparator": {"configure": {"background": pal.border_soft}},
        "Strong.TSeparator": {"configure": {"background": pal.border}},
        # ---- buttons ----
        "Button.button": {"element create": ("image", btn, ("disabled", btn_disabled), ("pressed", btn_press),
                                             ("active", btn_hover), ("focus", btn_focus), {"border": B, "sticky": "nswe"})},
        "TButton": {"layout": button_layout("Button.button"),
                    "configure": {"padding": pad_btn, "anchor": "center", "foreground": pal.fg},
                    "map": {"foreground": [("disabled", pal.fg_faint)]}},
        "Accent.button": {"element create": ("image", acc, ("disabled", acc_disabled), ("pressed", acc_press),
                                             ("active", acc_hover), ("focus", acc_focus), {"border": B, "sticky": "nswe"})},
        "Accent.TButton": {"layout": button_layout("Accent.button"),
                           "configure": {"padding": pad_btn, "anchor": "center", "foreground": pal.accent_fg,
                                         "font": fonts.ui_bold.name},
                           "map": {"foreground": [("disabled", mix(pal.accent_fg, pal.accent, 0.35))]}},
        "Destructive.button": {"element create": ("image", dstr, ("disabled", dstr_disabled), ("pressed", dstr_hover),
                                                  ("active", dstr_hover), {"border": B, "sticky": "nswe"})},
        "Destructive.TButton": {"layout": button_layout("Destructive.button"),
                                "configure": {"padding": pad_btn, "anchor": "center", "foreground": "#ffffff"},
                                "map": {"foreground": [("disabled", mix("#ffffff", pal.error_bg, 0.35))]}},
        "Toolbutton.button": {"element create": ("image", tool, ("disabled", tool), ("selected", tool_sel),
                                                 ("pressed", tool_press), ("active", tool_hover), {"border": B, "sticky": "nswe"})},
        "Toolbutton": {"layout": button_layout("Toolbutton.button"),
                       "configure": {"padding": (px(8), px(6)), "anchor": "center", "foreground": pal.fg},
                       "map": {"foreground": [("disabled", pal.fg_faint)]}},
        "Icon.Toolbutton": {"configure": {"padding": (px(6), px(6))}},
        "Tool.TCheckbutton": {"layout": [("Toolbutton.button", {"sticky": "nswe", "children": [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})],
            "configure": {"padding": (px(6), px(4)), "anchor": "center", "background": pal.window},
            "map": {"background": [("active", pal.window)]}},
        # ttk paints a widget's own background from its style, so flat controls
        # sitting on a non-window surface need a variant that matches it.
        "View.Toolbutton": {"configure": {"background": pal.view}},
        "View.Icon.Toolbutton": {"configure": {"background": pal.view}},
        "View.Tool.TCheckbutton": {"configure": {"background": pal.view}, "map": {"background": [("active", pal.view)]}},
        "Sidebar.Toolbutton": {"configure": {"background": pal.sidebar}},
        "Sidebar.Icon.Toolbutton": {"configure": {"background": pal.sidebar}},
        "Dialog.Toolbutton": {"configure": {"background": pal.dialog}},
        "Dialog.Icon.Toolbutton": {"configure": {"background": pal.dialog}},
        "Popover.Icon.Toolbutton": {"configure": {"background": pal.popover}},
        "View.TButton": {"configure": {"background": pal.view}},
        "Sidebar.TButton": {"configure": {"background": pal.sidebar}},
        "Sidebar.Accent.TButton": {"configure": {"background": pal.sidebar}},
        "View.Accent.TButton": {"configure": {"background": pal.view}},
        # ---- fields ----
        "Entry.field": {"element create": ("image", field, ("disabled", field_disabled), ("invalid", field_invalid),
                                           ("focus", field_focus), ("hover", field_hover), {"border": B, "sticky": "nswe"})},
        "TEntry": {"layout": [("Entry.field", {"sticky": "nswe", "children": [
            ("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})],
            "configure": {"padding": pad_field, "foreground": pal.fg, "insertcolor": pal.fg, "selectbackground": pal.selection,
                          "selectforeground": pal.fg},
            "map": {"foreground": [("disabled", pal.fg_faint)]}},
        "Placeholder.TEntry": {"configure": {"foreground": pal.fg_faint}},
        "View.TEntry": {"configure": {"background": pal.view}},
        "Sidebar.TEntry": {"configure": {"background": pal.sidebar}},
        "Popover.TEntry": {"configure": {"background": pal.popover}},
        "Combobox.field": {"element create": ("image", field, ("disabled", field_disabled), ("focus", field_focus),
                                              ("hover", field_hover), {"border": B, "sticky": "nswe"})},
        "Combobox.downarrow": {"element create": ("image", chev_d, ("disabled", chev_d_dim), {"sticky": "", "width": px(26)})},
        "TCombobox": {"layout": [("Combobox.field", {"sticky": "nswe", "children": [
            ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})],
            "configure": {"padding": pad_field, "foreground": pal.fg, "insertcolor": pal.fg, "arrowsize": px(26),
                          "selectbackground": pal.selection, "selectforeground": pal.fg},
            "map": {"foreground": [("disabled", pal.fg_faint), ("readonly", pal.fg)],
                    "fieldbackground": [("readonly", pal.view)],
                    "selectbackground": [("readonly", pal.view)], "selectforeground": [("readonly", pal.fg)]}},
        "Spinbox.field": {"element create": ("image", field, ("disabled", field_disabled), ("focus", field_focus),
                                             {"border": B, "sticky": "nswe"})},
        "Spinbox.uparrow": {"element create": ("image", chev_up, {"sticky": "", "width": px(22)})},
        "Spinbox.downarrow": {"element create": ("image", chev_d, {"sticky": "", "width": px(22)})},
        "TSpinbox": {"configure": {"padding": pad_field, "foreground": pal.fg, "insertcolor": pal.fg}},
        # ---- check / radio ----
        "Checkbutton.indicator": {"element create": (
            "image", cb_off, ("disabled", "selected", cb_on_dis), ("disabled", cb_dis),
            ("selected", "active", cb_on_hover), ("selected", cb_on), ("active", cb_off_hover),
            {"sticky": "", "padding": (0, 0, px(6), 0)})},
        "TCheckbutton": {"configure": {"padding": (px(2), px(3)), "background": pal.window, "foreground": pal.fg},
                         "map": {"background": [("active", pal.window)], "foreground": [("disabled", pal.fg_faint)]}},
        "Dialog.TCheckbutton": {"configure": {"background": pal.dialog}, "map": {"background": [("active", pal.dialog)]}},
        "View.TCheckbutton": {"configure": {"background": pal.view}, "map": {"background": [("active", pal.view)]}},
        "Radiobutton.indicator": {"element create": (
            "image", rb_off, ("disabled", rb_dis), ("selected", "active", rb_on_hover), ("selected", rb_on),
            ("active", rb_off_hover), {"sticky": "", "padding": (0, 0, px(6), 0)})},
        "TRadiobutton": {"configure": {"padding": (px(2), px(3)), "background": pal.window, "foreground": pal.fg},
                         "map": {"background": [("active", pal.window)], "foreground": [("disabled", pal.fg_faint)]}},
        "Dialog.TRadiobutton": {"configure": {"background": pal.dialog}, "map": {"background": [("active", pal.dialog)]}},
        # ---- notebook (underline tabs with a close button) ----
        "Notebook.tab": {"element create": ("image", tab, ("selected", "active", tab_sel_hover), ("selected", tab_sel),
                                            ("active", tab_hover), {"border": (px(4), px(4), px(4), px(4)), "sticky": "nswe"})},
        "Notebook.client": {"element create": ("image", client, {"border": 0, "sticky": "nswe"})},
        "Notebook.close": {"element create": ("image", close, ("pressed", "active", close_pressed), ("active", close_hot),
                                              {"sticky": "", "padding": (px(8), 0, 0, 0)})},
        "TNotebook": {"configure": {"background": pal.window, "tabmargins": 0, "borderwidth": 0, "padding": 0,
                                    "tabposition": "nw"}},
        "TNotebook.Tab": {"layout": [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "left", "sticky": ""})]})]})],
            "configure": {"padding": (px(14), px(7)), "foreground": pal.fg_muted, "font": ui_font},
            "map": {"foreground": [("selected", pal.fg), ("active", pal.fg)]}},
        "Main.TNotebook": {"configure": {"background": pal.window}},
        "Main.TNotebook.Tab": {"layout": [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "left", "sticky": ""}),
                ("Notebook.close", {"side": "left", "sticky": ""})]})]})],
            "configure": {"padding": (px(14), px(8)), "foreground": pal.fg_muted, "font": ui_font},
            "map": {"foreground": [("selected", pal.fg), ("active", pal.fg)]}},
        "Results.TNotebook": {"configure": {"background": pal.view}},
        "Results.TNotebook.Tab": {"configure": {"padding": (px(12), px(6)), "font": fonts.small.name}},
        # ---- treeview ----
        "Treeitem.indicator": {"element create": ("image", chev_r, ("user2", empty), ("user1", chev_d),
                                                  {"sticky": "", "width": px(20)})},
        "Treeview": {"configure": {"rowheight": px(26), "background": pal.view, "fieldbackground": pal.view,
                                   "foreground": pal.fg, "borderwidth": 0, "relief": "flat", "lightcolor": pal.view,
                                   "darkcolor": pal.view, "bordercolor": pal.view, "indent": px(18)},
                     "map": {"background": [("selected", pal.accent)], "foreground": [("selected", pal.accent_fg)]}},
        "Sidebar.Treeview": {"configure": {"background": pal.sidebar, "fieldbackground": pal.sidebar,
                                           "lightcolor": pal.sidebar, "darkcolor": pal.sidebar, "bordercolor": pal.sidebar}},
        "Treeview.Heading": {"configure": {"background": pal.sidebar, "foreground": pal.fg_muted, "relief": "flat",
                                           "borderwidth": 0, "padding": (px(8), px(6)), "font": fonts.small_bold.name,
                                           "lightcolor": pal.sidebar, "darkcolor": pal.sidebar, "bordercolor": pal.border_soft},
                             "map": {"background": [("active", pal.hover)]}},
        "Treeview.Item": {"configure": {"padding": (px(2), 0)}},
        "Popover.Treeview": {"configure": {"background": pal.popover, "fieldbackground": pal.popover,
                                           "lightcolor": pal.popover, "darkcolor": pal.popover, "bordercolor": pal.popover,
                                           "rowheight": px(28)},
                             "map": {"background": [("selected", pal.accent_soft)], "foreground": [("selected", pal.fg)]}},
        # ---- scrollbars ----
        "Vertical.Scrollbar.thumb": {"element create": ("image", thumb_v, ("pressed", thumb_v_hover), ("active", thumb_v_hover),
                                                        {"border": (0, B, 0, B), "sticky": "ns"})},
        "Vertical.Scrollbar.trough": {"element create": ("image", trough_v, {"border": 0, "sticky": "ns"})},
        "Vertical.TScrollbar": {"layout": [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})]},
        "Horizontal.Scrollbar.thumb": {"element create": ("image", thumb_h, ("pressed", thumb_h_hover), ("active", thumb_h_hover),
                                                          {"border": (B, 0, B, 0), "sticky": "we"})},
        "Horizontal.Scrollbar.trough": {"element create": ("image", im.flat("trough_h", sb, None), {"border": 0, "sticky": "we"})},
        "Horizontal.TScrollbar": {"layout": [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [
            ("Horizontal.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})]},
        "TScrollbar": {"configure": {"background": pal.window}},
        "View.Vertical.TScrollbar": {"configure": {"background": pal.view}},
        "View.Horizontal.TScrollbar": {"configure": {"background": pal.view}},
        "Sidebar.Vertical.TScrollbar": {"configure": {"background": pal.sidebar}},
        "Sidebar.Horizontal.TScrollbar": {"configure": {"background": pal.sidebar}},
        "Popover.Vertical.TScrollbar": {"configure": {"background": pal.popover}},
        # ---- progress ----
        "Horizontal.Progressbar.pbar": {"element create": ("image", pbar, {"border": (px(3), 0, px(3), 0), "sticky": "we"})},
        "Horizontal.Progressbar.trough": {"element create": ("image", ptrough, {"border": (px(3), 0, px(3), 0), "sticky": "we"})},
        "Horizontal.TProgressbar": {"layout": [("Horizontal.Progressbar.trough", {"sticky": "nswe", "children": [
            ("Horizontal.Progressbar.pbar", {"side": "left", "sticky": "ns"})]})],
            "configure": {"background": pal.window, "troughcolor": pal.border_soft, "borderwidth": 0}},
        "Status.Horizontal.TProgressbar": {"configure": {"background": pal.sidebar}},
        # ---- paned windows ----
        "TPanedwindow": {"configure": {"background": pal.window}},
        "Sash": {"configure": {"sashthickness": px(6), "gripcount": 0}},
        # ---- misc ----
        "TLabelframe": {"configure": {"background": pal.window, "bordercolor": pal.border, "lightcolor": pal.window,
                                      "darkcolor": pal.window, "relief": "solid", "borderwidth": 1}},
        "TLabelframe.Label": {"configure": {"background": pal.window, "foreground": pal.fg_muted, "font": fonts.small_bold.name}},
        "TMenubutton": {"configure": {"padding": pad_btn, "background": pal.view, "foreground": pal.fg,
                                      "arrowcolor": pal.fg_muted}},
        "TSizegrip": {"configure": {"background": pal.sidebar}},
    }
    return settings


# =============================================================================
# Theme: the object the app talks to
# =============================================================================

class Theme:
    """Owns the palette, fonts, icons and the ttk themes; applies colours to
    non-ttk widgets on request; notifies listeners when the mode changes."""

    MODES = ("system", "light", "dark")

    def __init__(self, root):
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96.0
        self.fonts = Fonts(root)
        self.icons = IconSet(root)
        self.style = ttk.Style(root)
        self.mode = "system"
        self.pal = LIGHT
        self._built = {}
        self._images = {}
        self.listeners = []
        self._system_dark = None

    # ---- metrics ----
    def px(self, n):
        return max(1, int(round(n * self.scale)))

    # ---- mode ----
    def resolve_dark(self, mode):
        if mode == "dark":
            return True
        if mode == "light":
            return False
        if self._system_dark is None:
            self._system_dark = bool(detect_system_dark())
        return self._system_dark

    def set_mode(self, mode, notify=True):
        if mode not in self.MODES:
            mode = "system"
        self.mode = mode
        dark = self.resolve_dark(mode)
        pal = DARK if dark else LIGHT
        name = f"notepadb-{pal.name}"
        if name not in self._built:
            store = _Images(self.root, f"np_{pal.name}_")
            settings = _theme_settings(pal, self.px, self.fonts, store, self.icons)
            self.style.theme_create(name, parent="clam", settings=settings)
            self._built[name] = store
        self.pal = pal
        self.style.theme_use(name)
        self._apply_option_db()
        try:
            self.root.configure(background=pal.window)
        except tk.TclError:
            pass
        if notify:
            for fn in list(self.listeners):
                try:
                    fn(pal)
                except tk.TclError:
                    pass

    @property
    def dark(self):
        return self.pal.dark

    def add_listener(self, fn):
        self.listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self.listeners:
            self.listeners.remove(fn)

    # ---- icons ----
    def icon(self, name, role="fg", size=16, pad_right=0):
        """PhotoImage for `name` in palette colour `role` (or a hex colour), `size` in logical px."""
        color = self.pal[role] if role in self.pal else role
        return self.icons.get(name, color, self.px(size), self.px(pad_right) if pad_right else 0)

    def icon_states(self, name, size=16, role="fg", disabled_role="fg_faint"):
        """A ttk image state spec: normal + disabled variant."""
        return (self.icon(name, role, size), "disabled", self.icon(name, disabled_role, size))

    def app_icons(self):
        # Window icons only; 64 px is plenty for task switchers and costs ~60 ms
        # to rasterize, whereas 256 px would add a full second to startup.
        return [self.icons.get("logo", LIGHT.accent, s) for s in (16, 24, 32, 48, 64)]

    # ---- styling helpers for classic Tk widgets ----
    def _apply_option_db(self):
        pal, f = self.pal, self.fonts
        add = self.root.option_add
        for pat, val in (
            ("*Menu.background", pal.popover), ("*Menu.foreground", pal.fg),
            ("*Menu.activeBackground", pal.accent), ("*Menu.activeForeground", pal.accent_fg),
            ("*Menu.activeBorderWidth", 0), ("*Menu.borderWidth", 1), ("*Menu.relief", "flat"),
            ("*Menu.font", f.ui.name), ("*Menu.selectColor", pal.accent_text),
            ("*Menu.disabledForeground", pal.fg_faint),
            ("*Listbox.background", pal.view), ("*Listbox.foreground", pal.fg),
            ("*Listbox.selectBackground", pal.accent), ("*Listbox.selectForeground", pal.accent_fg),
            ("*Listbox.highlightThickness", 0), ("*Listbox.borderWidth", 0), ("*Listbox.font", f.ui.name),
            ("*Listbox.activeStyle", "none"),
            ("*TCombobox*Listbox.background", pal.popover), ("*TCombobox*Listbox.foreground", pal.fg),
            ("*TCombobox*Listbox.selectBackground", pal.accent), ("*TCombobox*Listbox.selectForeground", pal.accent_fg),
            ("*TCombobox*Listbox.font", f.ui.name), ("*TCombobox*Listbox.borderWidth", 0),
            ("*TCombobox*Listbox.highlightThickness", 1), ("*TCombobox*Listbox.highlightColor", pal.border),
            ("*TCombobox*Listbox.highlightBackground", pal.border),
            ("*Toplevel.background", pal.window), ("*Frame.background", pal.window),
            ("*Canvas.background", pal.view), ("*Canvas.highlightThickness", 0),
            ("*Text.background", pal.view), ("*Text.foreground", pal.fg),
            ("*Text.selectBackground", pal.selection), ("*Text.selectForeground", pal.fg),
            ("*Text.insertBackground", pal.caret), ("*Text.highlightThickness", 0), ("*Text.borderWidth", 0),
            ("*Text.font", f.mono.name),
            ("*Dialog.msg.font", f.ui.name), ("*Dialog.dtl.font", f.ui.name),
        ):
            try:
                add(pat, val)
            except tk.TclError:
                pass

    def style_menu(self, menu):
        pal, f = self.pal, self.fonts
        try:
            menu.configure(
                background=pal.popover, foreground=pal.fg, activebackground=pal.accent,
                activeforeground=pal.accent_fg, activeborderwidth=0, borderwidth=1, relief="flat",
                font=f.ui.name, selectcolor=pal.accent_text, disabledforeground=pal.fg_faint,
            )
        except tk.TclError:
            pass
        try:
            end = menu.index("end")
        except tk.TclError:
            end = None
        if end is None:
            return
        for i in range(end + 1):
            try:
                if menu.type(i) == "cascade":
                    sub = menu.nametowidget(menu.entrycget(i, "menu"))
                    self.style_menu(sub)
            except (tk.TclError, KeyError):
                pass

    def style_listbox(self, lb, mono=False, bg=None):
        pal, f = self.pal, self.fonts
        lb.configure(
            background=bg or pal.view, foreground=pal.fg, selectbackground=pal.accent,
            selectforeground=pal.accent_fg, highlightthickness=0, borderwidth=0, relief="flat",
            activestyle="none", font=(f.mono if mono else f.ui).name,
        )

    def style_editor(self, text):
        pal, f = self.pal, self.fonts
        text.configure(
            background=pal.view, foreground=pal.fg, insertbackground=pal.caret, insertwidth=self.px(2),
            selectbackground=pal.selection, selectforeground=pal.fg, inactiveselectbackground=pal.selection_inactive,
            highlightthickness=0, borderwidth=0, relief="flat", font=f.mono.name,
            padx=self.px(10), pady=self.px(8), spacing1=self.px(1), spacing3=self.px(1), tabs=(f.mono.measure("    "),),
        )
        text.tag_configure("kw", foreground=pal.syn_kw, font=f.mono_bold.name)
        text.tag_configure("str", foreground=pal.syn_str)
        text.tag_configure("comment", foreground=pal.syn_comment)
        text.tag_configure("num", foreground=pal.syn_num)
        text.tag_configure("bracket", background=pal.syn_bracket)
        text.tag_configure("curline", background=pal.current_line)
        text.tag_configure("findmatch", background=pal.find_match)
        text.tag_lower("curline")
        text.tag_raise("findmatch")
        text.tag_raise("bracket")
        text.tag_raise("sel")

    def style_gutter(self, text):
        pal, f = self.pal, self.fonts
        text.configure(
            background=pal.view, foreground=pal.gutter_fg, highlightthickness=0, borderwidth=0, relief="flat",
            font=f.mono.name, padx=self.px(6), pady=self.px(8), spacing1=self.px(1), spacing3=self.px(1),
            state="disabled", cursor="arrow", takefocus=0, insertwidth=0,
            selectbackground=pal.view, selectforeground=pal.gutter_fg,
        )
        text.tag_configure("right", justify="right")
        text.tag_configure("current", foreground=pal.gutter_current)

    def style_log(self, text):
        pal, f = self.pal, self.fonts
        text.configure(
            background=pal.view, foreground=pal.fg, highlightthickness=0, borderwidth=0, relief="flat",
            font=f.mono_small.name, padx=self.px(10), pady=self.px(8), spacing1=self.px(1), spacing3=self.px(1),
            selectbackground=pal.selection, selectforeground=pal.fg, inactiveselectbackground=pal.selection_inactive,
            insertbackground=pal.caret, cursor="arrow", wrap="word",
        )
        text.tag_configure("time", foreground=pal.fg_faint)
        text.tag_configure("error", foreground=pal.error)
        text.tag_configure("ok", foreground=pal.success)
        text.tag_configure("muted", foreground=pal.fg_muted)
        text.tag_configure("sql", foreground=pal.fg_muted)

    def style_preview(self, text):
        pal, f = self.pal, self.fonts
        text.configure(
            background=pal.view, foreground=pal.fg, highlightthickness=1, highlightbackground=pal.border,
            highlightcolor=pal.border, borderwidth=0, relief="flat", font=f.mono_small.name,
            padx=self.px(10), pady=self.px(8), selectbackground=pal.selection, selectforeground=pal.fg,
            inactiveselectbackground=pal.selection_inactive, insertbackground=pal.caret, wrap="none",
        )
        text.tag_configure("kw", foreground=pal.syn_kw, font=f.mono_small_bold.name)
        text.tag_configure("str", foreground=pal.syn_str)
        text.tag_configure("comment", foreground=pal.syn_comment)
        text.tag_configure("num", foreground=pal.syn_num)

    def sheet_options(self):
        """tksheet options matching the palette (same keys as tksheet's own themes)."""
        pal, f = self.pal, self.fonts
        mono = f.tuple(f.mono_small, "normal")
        mono_bold = f.tuple(f.mono_small, "bold")
        ui = f.tuple(f.small, "normal")
        return dict(
            font=mono, header_font=mono_bold, index_font=ui, popup_menu_font=f.tuple(f.ui, "normal"),
            frame_bg=pal.view, outline_color=pal.view, outline_thickness=0,
            popup_menu_fg=pal.fg, popup_menu_bg=pal.popover, popup_menu_highlight_bg=pal.accent,
            popup_menu_highlight_fg=pal.accent_fg,
            index_hidden_rows_expander_bg=pal.fg_faint, header_hidden_columns_expander_bg=pal.fg_faint,
            header_bg=pal.grid_header_bg, header_border_fg=pal.grid_line, header_grid_fg=pal.grid_line,
            header_fg=pal.grid_header_fg, header_editor_bg=pal.view, header_editor_fg=pal.fg,
            header_editor_select_bg=pal.selection, header_editor_select_fg=pal.fg,
            header_selected_cells_bg=pal.grid_sel, header_selected_cells_fg=pal.fg,
            index_bg=pal.grid_index_bg, index_border_fg=pal.grid_line, index_grid_fg=pal.grid_line,
            index_fg=pal.grid_index_fg, index_editor_bg=pal.view, index_editor_fg=pal.fg,
            index_editor_select_bg=pal.selection, index_editor_select_fg=pal.fg,
            index_selected_cells_bg=pal.grid_sel, index_selected_cells_fg=pal.fg,
            top_left_bg=pal.grid_header_bg, top_left_fg=pal.grid_line, top_left_fg_highlight=pal.fg_muted,
            table_bg=pal.view, table_grid_fg=pal.grid_line, table_fg=pal.fg,
            table_editor_bg=pal.view, table_editor_fg=pal.fg, table_editor_select_bg=pal.selection,
            table_editor_select_fg=pal.fg,
            table_selected_box_cells_fg=pal.grid_sel_border, table_selected_box_rows_fg=pal.grid_sel_border,
            table_selected_box_columns_fg=pal.grid_sel_border, table_selected_cells_border_fg=pal.grid_sel_border,
            table_selected_cells_bg=pal.grid_sel, table_selected_cells_fg=pal.fg,
            resizing_line_fg=pal.accent, drag_and_drop_bg=pal.accent,
            header_selected_columns_bg=pal.accent, header_selected_columns_fg=pal.accent_fg,
            index_selected_rows_bg=pal.accent, index_selected_rows_fg=pal.accent_fg,
            table_selected_rows_border_fg=pal.grid_sel_border, table_selected_rows_bg=pal.grid_sel,
            table_selected_rows_fg=pal.fg, table_selected_columns_border_fg=pal.grid_sel_border,
            table_selected_columns_bg=pal.grid_sel, table_selected_columns_fg=pal.fg,
            tree_arrow_fg=pal.fg_muted, selected_cells_tree_arrow_fg=pal.fg, selected_rows_tree_arrow_fg=pal.accent_fg,
            vertical_scroll_background=pal.view, horizontal_scroll_background=pal.view,
            vertical_scroll_troughcolor=pal.view, horizontal_scroll_troughcolor=pal.view,
            vertical_scroll_lightcolor=pal.view, horizontal_scroll_lightcolor=pal.view,
            vertical_scroll_darkcolor=pal.view, horizontal_scroll_darkcolor=pal.view,
            vertical_scroll_relief="flat", horizontal_scroll_relief="flat",
            vertical_scroll_troughrelief="flat", horizontal_scroll_troughrelief="flat",
            vertical_scroll_bordercolor=pal.view, horizontal_scroll_bordercolor=pal.view,
            vertical_scroll_active_bg=pal.scroll_thumb_hover, horizontal_scroll_active_bg=pal.scroll_thumb_hover,
            vertical_scroll_not_active_bg=pal.scroll_thumb, horizontal_scroll_not_active_bg=pal.scroll_thumb,
            vertical_scroll_pressed_bg=pal.scroll_thumb_hover, horizontal_scroll_pressed_bg=pal.scroll_thumb_hover,
            vertical_scroll_active_fg=pal.scroll_thumb_hover, horizontal_scroll_active_fg=pal.scroll_thumb_hover,
            vertical_scroll_not_active_fg=pal.scroll_thumb, horizontal_scroll_not_active_fg=pal.scroll_thumb,
            vertical_scroll_pressed_fg=pal.scroll_thumb_hover, horizontal_scroll_pressed_fg=pal.scroll_thumb_hover,
            alternate_color=pal.grid_zebra,
        )


# =============================================================================
# Demo
# =============================================================================

if __name__ == "__main__":  # pragma: no cover - visual smoke test
    root = tk.Tk()
    root.title("notepaDB theme demo")
    theme = Theme(root)
    theme.set_mode(sys.argv[1] if len(sys.argv) > 1 else "system")
    pal = theme.pal
    root.geometry("980x600")
    bar = ttk.Frame(root, padding=theme.px(10))
    bar.pack(fill="x")
    ttk.Button(bar, text=" Run", image=theme.icon("play", "accent_fg"), compound="left", style="Accent.TButton").pack(side="left", padx=4)
    ttk.Button(bar, text=" Stop", image=theme.icon_states("stop"), compound="left", state="disabled").pack(side="left", padx=4)
    ttk.Button(bar, text="Connect", image=theme.icon("plug"), compound="left", style="Toolbutton").pack(side="left", padx=4)
    ttk.Button(bar, image=theme.icon("refresh"), style="Icon.Toolbutton").pack(side="left")
    ttk.Button(bar, text="Delete", style="Destructive.TButton").pack(side="left", padx=4)
    e = ttk.Entry(bar, width=20); e.insert(0, "Filter tables…"); e.pack(side="left", padx=4)
    cb = ttk.Combobox(bar, values=["SQLite", "PostgreSQL"], state="readonly", width=14); cb.set("SQLite"); cb.pack(side="left", padx=4)
    v = tk.BooleanVar(value=True)
    ttk.Checkbutton(bar, text="Encrypt", variable=v).pack(side="left", padx=4)
    ttk.Checkbutton(bar, text="Trust").pack(side="left", padx=4)
    rv = tk.StringVar(value="a")
    ttk.Radiobutton(bar, text="Windows", variable=rv, value="a").pack(side="left")
    ttk.Radiobutton(bar, text="SQL Login", variable=rv, value="b").pack(side="left")
    pb = ttk.Progressbar(bar, mode="indeterminate", length=120); pb.pack(side="left", padx=8); pb.start(12)
    pw = ttk.PanedWindow(root, orient="horizontal"); pw.pack(fill="both", expand=True, padx=theme.px(10), pady=(0, theme.px(10)))
    left = ttk.Frame(pw, style="Card.TFrame", padding=1); pw.add(left, weight=0)
    tree = ttk.Treeview(left, show="tree", style="Sidebar.Treeview"); tree.pack(fill="both", expand=True, side="left")
    sbv = ttk.Scrollbar(left, orient="vertical", command=tree.yview); sbv.pack(side="right", fill="y"); tree.configure(yscrollcommand=sbv.set)
    tree.tag_configure("db", image=theme.icon("database", "accent_text", pad_right=6)); tree.tag_configure("t", image=theme.icon("table", "fg_muted", pad_right=6)); tree.tag_configure("c", image=theme.icon("column", "fg_faint", pad_right=6))
    db = tree.insert("", "end", text="baseball.db", open=True, tags=("db",))
    t1 = tree.insert(db, "end", text="players", open=True, tags=("t",))
    for c in ["player_id  integer", "first_name  text", "team_id  integer"]: tree.insert(t1, "end", text=c, tags=("c",))
    tree.insert(db, "end", text="teams", tags=("t",)); tree.insert(db, "end", text="games", tags=("t",))
    for i in range(30): tree.insert(db, "end", text=f"table_{i}", tags=("t",))
    right = ttk.Frame(pw, style="Card.TFrame", padding=1); pw.add(right, weight=1)
    nb = ttk.Notebook(right, style="Main.TNotebook"); nb.pack(fill="both", expand=True)
    f1 = ttk.Frame(nb, style="View.TFrame"); f2 = ttk.Frame(nb, style="View.TFrame"); nb.add(f1, text="baseball1 •"); nb.add(f2, text="players DDL")
    t = tk.Text(f1); theme.style_editor(t); t.pack(fill="both", expand=True)
    t.insert("1.0", "SELECT p.first_name, t.team_name  -- comment\nFROM players p JOIN teams t ON t.team_id = p.team_id\nWHERE p.jersey_number > 10 AND t.city = 'Houston';")
    for n in theme.icons._images: pass
    icons_row = ttk.Frame(root, padding=(theme.px(10), 0, theme.px(10), theme.px(10))); icons_row.pack(fill="x")
    for name in ICONS:
        ttk.Label(icons_row, image=theme.icon(name, "fg", 20)).pack(side="left", padx=3)
    ttk.Label(icons_row, image=theme.icon("logo", "accent", 48)).pack(side="left", padx=8)
    root.mainloop()
