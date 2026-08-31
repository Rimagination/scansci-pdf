"""Light frosted-glass floating progress bar.

A tiny always-on-top desktop widget that watches the task progress file
(``progress_reporter``) and shows: task name, current item, a rounded pill
progress bar, and success/fail counters. Drag the client area to move. The
native title bar provides minimize, maximize, and close controls.

Usage: ``scansci-pdf progress`` (or ``python -m scansci_pdf.progress_bar``).

Visual stack (no third-party GUI deps):
- The process is per-monitor DPI aware and all sizes scale by the real DPI
  factor — crisp text instead of Windows' bitmap stretch.
- DWM ``DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_MAINWINDOW`` provides the light
  Mica material; systems without that attribute use a white translucent card.
- All text is laid out with measured widths (CJK-safe), never character-count
  guesses.
"""

from __future__ import annotations

import os
import tkinter as tk
import tkinter.font as tkfont
from typing import Any

import os as _os

from .progress_reporter import read_state

# --- Win32 / DWM constants -------------------------------------------------
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWCP_ROUND = 2
DWMSBT_MAINWINDOW = 2  # light Mica

GLASS = "#f7f9fc"
TRACK = "#cbd5e1"
FILL = "#2563eb"
FILL_OK = "#16834b"
TEXT = "#0f172a"
DIM = "#475569"
ATTENTION = "#b45309"
OK_GREEN = "#1b7d46"
BAD_RED = "#c0392b"

# Logical layout (scaled by the real DPI factor at runtime)
W, H, MARGIN = 440, 122, 12
RADIUS = 16


def _enable_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dwm(hwnd: int, attr: int, value: int) -> bool:
    try:
        import ctypes
        import ctypes.wintypes as wt
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        v = ctypes.c_int(value)
        r = dwm.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(v), ctypes.sizeof(v),
        )
        return r == 0
    except Exception:
        return False


def _dwm_color(hwnd: int, attr: int, color: int) -> bool:
    """Set a DWM COLORREF attribute when the current Windows build supports it."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        value = ctypes.c_uint(color)
        return dwm.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(value), ctypes.sizeof(value),
        ) == 0
    except Exception:
        return False


def _elide(font: tkfont.Font, text: str, width_px: int) -> str:
    if font.measure(text) <= width_px:
        return text
    while text and font.measure(text + "…") > width_px:
        text = text[:-1]
    return text + "…"


class ProgressWindow:
    def __init__(self) -> None:
        _enable_dpi_awareness()
        self.root = tk.Tk()
        self.s = max(1.0, self.root.winfo_fpixels("1i") / 96.0)  # DPI factor
        self.W, self.H, self.M = int(W * self.s), int(H * self.s), int(MARGIN * self.s)
        self.root.title("ScanSci PDF · 进度")
        self.root.geometry(f"{self.W}x{self.H}+{self._anchor_x()}-{self.H + 8}")
        self.root.minsize(self.W, self.H)
        self.root.resizable(True, True)
        self.root.attributes("-topmost", True)
        # Keep the native title bar enabled so Windows supplies minimize,
        # maximize, and close controls.
        self.root.configure(bg=GLASS)

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H,
                                bg=GLASS, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Map the Tk popup before asking Win32 for its top-level handle.
        self.root.update_idletasks()
        self.root.update()
        self._apply_backdrop()
        self._fonts()
        self._bind_mouse()
        self._state_shown_at = 0.0
        self.root.after(400, self._tick)

    # --- window plumbing ---------------------------------------------------
    def _hwnd(self) -> int:
        try:
            import ctypes
            import ctypes.wintypes as wt
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            # Tk returns the top-level HWND for the root window here.  Calling
            # GetParent on it returns 0, which used to disable every DWM path.
            user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
            user32.GetAncestor.restype = wt.HWND
            GA_ROOT = 2
            hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
            return int(hwnd or self.root.winfo_id())
        except Exception:
            return 0

    def _apply_backdrop(self) -> None:
        hwnd = self._hwnd()
        self._acrylic = False
        # ponytail: Tk Canvas does not preserve readable item colors over an
        # extended DWM frame, so use a light translucent client surface. The
        # native DWM material still styles the window/title bar when present.
        self.root.configure(bg=GLASS)
        self.canvas.configure(bg=GLASS)
        try:
            self.root.attributes("-alpha", 0.94)
        except Exception:
            pass
        if not hwnd:
            return
        try:
            # Force the light DWM material so the widget stays white even when
            # the desktop is using dark mode.
            _dwm(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 0)
            _dwm(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)
            # Keep the native title bar in the same light palette as the
            # frosted client area. COLORREF values are 0x00BBGGRR.
            _dwm_color(hwnd, DWMWA_CAPTION_COLOR, 0x00FFFFFF)
            _dwm_color(hwnd, DWMWA_BORDER_COLOR, 0x00ECE2D9)
            _dwm_color(hwnd, DWMWA_TEXT_COLOR, 0x0037291F)
        except Exception:
            pass
        _dwm(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)

    def _fonts(self) -> None:
        s = self.s
        self.f_title = tkfont.Font(family="Segoe UI", size=int(-12 * s), weight="bold")
        self.f_phase = tkfont.Font(family="Segoe UI", size=int(-10 * s))
        self.f_small = tkfont.Font(family="Segoe UI", size=int(-10 * s))
        self.f_pct = tkfont.Font(family="Segoe UI", size=int(-12 * s), weight="bold")

    def _anchor_x(self) -> int:
        try:
            import ctypes
            return int(ctypes.windll.user32.GetSystemMetrics(0)) - self.W - 24
        except Exception:
            return 24

    def _bind_mouse(self) -> None:
        self._drag_dx = self._drag_dy = 0
        c = self.canvas
        c.bind("<Button-1>", self._press)
        c.bind("<B1-Motion>", self._move)

    def _press(self, event: Any) -> None:
        self._drag_dx, self._drag_dy = event.x, event.y

    def _move(self, event: Any) -> None:
        x = self.root.winfo_x() + event.x - self._drag_dx
        y = self.root.winfo_y() + event.y - self._drag_dy
        self.root.geometry(f"+{x}+{y}")

    # --- drawing ------------------------------------------------------------
    def _rounded_rect(self, x0: int, y0: int, x1: int, y1: int, r: int, **kw: Any) -> Any:
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kw)

    def _draw(self, state: dict[str, Any] | None) -> None:
        c = self.canvas
        c.delete("all")
        M = self.M
        Wl = max(self.W, self.canvas.winfo_width())
        Hl = max(self.H, self.canvas.winfo_height())

        # White translucent card with a subtle outline keeps the Tk text
        # readable while the DWM material shows through the window alpha.
        self._rounded_rect(M, M, Wl - M, Hl - M, int(RADIUS * self.s),
                           fill=GLASS, outline=TRACK)

        if not state:
            c.create_text(Wl // 2, Hl // 2, text="ScanSci · 待命", fill=DIM,
                          font=self.f_phase)
            return

        running = state.get("status") == "running"
        total = max(0, int(state.get("total", 0) or 0))
        done = min(total, int(state.get("done", 0) or 0)) if total else int(state.get("done", 0) or 0)
        ok = int(state.get("success", 0) or 0)
        bad = int(state.get("failed", 0) or 0)
        pct = (done / total) if total else 0.0
        task = str(state.get("task") or "任务")
        phase = str(state.get("phase") or "")
        current = str(state.get("current") or ("" if running else "完成"))

        top = M + int(14 * self.s)
        # title (measured width — CJK safe), phase right after it
        c.create_text(M + int(14 * self.s), top, text=task, fill=TEXT,
                      font=self.f_title, anchor="w")
        x_after = M + int(14 * self.s) + self.f_title.measure(task) + int(8 * self.s)
        if phase:
            c.create_text(x_after, top, text=phase, fill=DIM,
                          font=self.f_phase, anchor="w")

        # right cluster: ✓ok ✗bad  |  pct (window controls live in the
        # native title bar and remain available while the content is resized)
        x_pct = Wl - M - int(12 * self.s)
        c.create_text(x_pct, top, text=f"{pct * 100:.0f}%", fill=FILL,
                      font=self.f_pct, anchor="e")
        x_bad = x_pct - self.f_pct.measure(f"{pct * 100:.0f}%") - int(12 * self.s)
        c.create_text(x_bad, top, text=f"✗{bad}", fill=BAD_RED,
                      font=self.f_small, anchor="e")
        x_ok = x_bad - self.f_small.measure(f"✗{bad}") - int(8 * self.s)
        c.create_text(x_ok, top, text=f"✓{ok}", fill=OK_GREEN,
                      font=self.f_small, anchor="e")

        # Current item, or a persistent in-progress reminder for a browser
        # challenge. The challenged DOI is not advanced until its page clears.
        cur_y = top + int(24 * self.s)
        avail = Wl - 2 * M - int(28 * self.s)
        attention = state.get("attention")
        attention_items: list[dict[str, Any]] = []
        if isinstance(attention, dict):
            if "message" in attention:
                attention_items = [attention]
            else:
                attention_items = [v for v in attention.values() if isinstance(v, dict)]
        elif isinstance(attention, list):
            attention_items = [v for v in attention if isinstance(v, dict)]
        if attention_items:
            item = attention_items[-1]
            notice = str(item.get("message") or "请在浏览器窗口完成安全验证")
            doi = str(item.get("current") or current or "")
            text = f"[需人工验证] {notice}"
            if doi:
                text += f" · {doi}"
            if len(attention_items) > 1:
                text += f"（另有 {len(attention_items) - 1} 项）"
            c.create_text(M + int(14 * self.s), cur_y,
                          text=_elide(self.f_small, text, avail),
                          fill=ATTENTION, font=self.f_small, anchor="w")
        elif current:
            c.create_text(M + int(14 * self.s), cur_y,
                          text=_elide(self.f_small, current, avail),
                          fill=DIM, font=self.f_small, anchor="w")

        # Output folder row: clickable, opens the downloads folder in Explorer.
        output_dir = str(state.get("output_dir") or "")
        out_y = cur_y + int(22 * self.s)
        if output_dir:
            # Segoe MDL2 Assets (ships with Win10/11) renders the standard
            # folder glyph U+E8B7 on the text baseline - crisp and aligned.
            icon_font = tkfont.Font(family="Segoe MDL2 Assets", size=int(-10 * self.s))
            out_x = M + int(14 * self.s)
            c.create_text(out_x, out_y, text="", fill=DIM,
                          font=icon_font, anchor="w", tags=("outdir",))
            text_x = out_x + icon_font.measure("") + int(4 * self.s)
            c.tag_bind("outdir", "<Button-1>",
                       lambda e: self._open_output_dir(state))
            c.tag_bind("outdir", "<Enter>", lambda e: c.config(cursor="hand2"))
            c.tag_bind("outdir", "<Leave>", lambda e: c.config(cursor=""))
            c.create_text(text_x, out_y,
                          text=_elide(self.f_small, output_dir, Wl - M - int(8 * self.s) - text_x),
                          fill=DIM, font=self.f_small, anchor="w", tags=("outdir",))

        # pill progress bar
        bx0, by1 = M + int(14 * self.s), Hl - M - int(14 * self.s)
        bx1, by0 = Wl - M - int(14 * self.s), by1 - int(18 * self.s)
        r = int(9 * self.s)
        self._rounded_rect(bx0, by0, bx1, by1, r, fill=TRACK, outline="")
        fill_w = int((bx1 - bx0) * pct)
        if fill_w > 2 * r:
            self._rounded_rect(bx0, by0, bx0 + fill_w, by1, r,
                               fill=(FILL_OK if not running else FILL), outline="")

    # --- polling ------------------------------------------------------------
    def _open_output_dir(self, state: dict) -> None:
        d = str(state.get("output_dir") or "")
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # Windows: open Explorer at the folder
            except Exception:
                pass

    def _tick(self) -> None:
        try:
            self._draw(read_state())
        except Exception:
            pass
        self.root.after(500, self._tick)

    def _report_geom(self) -> None:
        try:
            print(
                f"GEOM {self.root.winfo_x()} {self.root.winfo_y()} "
                f"{self.root.winfo_width()} {self.root.winfo_height()} "
                f"acrylic={int(getattr(self, '_acrylic', False))}",
                flush=True,
            )
        except Exception:
            pass

    def _write_lock(self) -> None:
        try:
            from .progress_reporter import progress_path

            lock = progress_path().parent / "bar.pid"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(_os.getpid()), encoding="utf-8")
        except Exception:
            pass

    def _release_lock(self) -> None:
        try:
            from .progress_reporter import progress_path

            lock = progress_path().parent / "bar.pid"
            if lock.exists():
                lock.unlink()
        except Exception:
            pass

    def run(self) -> None:
        self._write_lock()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(400, self._report_geom)
        self.root.mainloop()

    def _close(self) -> None:
        self._release_lock()
        self.root.destroy()


def main() -> None:
    try:
        ProgressWindow().run()
    except Exception as exc:  # headless / non-Windows fallback: console tail
        print(f"progress bar unavailable ({exc}); watching file…")
        import time as _t
        last = None
        while True:
            s = read_state()
            line = None
            if s:
                line = (f"{s.get('task')} {s.get('done')}/{s.get('total')} "
                        f"ok={s.get('success')} fail={s.get('failed')} {s.get('current')}")
            if line != last:
                print(line or "(idle)", flush=True)
                last = line
            _t.sleep(1)


if __name__ == "__main__":
    main()
