"""LazyScholar desktop shell (v4).

Owns everything that is *window chrome*, not business logic:
- single instance (named mutex + local-socket wake-up)
- the floating ball: a native Win32 layered window (per-pixel alpha).
  pywebview's `transparent` is documented as unsupported on Windows,
  which is why the v3 ball rendered on a white square; a layered
  window painted with UpdateLayeredWindow is the standard fix.
- the system tray (pystray, run_detached so pywebview keeps the
  main thread)
- the panel's slide animation and DWM rounded corners
- AppController: a command queue that tray / ball / JS bridge /
  wake listener all post to, so no component calls another directly.

Windows-only pieces degrade to no-ops elsewhere (dev / headless use).
"""
from __future__ import annotations

import ctypes
import socket
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

WIN = sys.platform == "win32"

WAKE_PORT = 47811
WAKE_MAGIC = b"lazyscholar:show"
MUTEX_NAME = r"Local\LazyScholar-SingleInstance-7F3A"
ERROR_ALREADY_EXISTS = 183

BALL_SIZE = 72          # logical px of the ball window
BALL_RENDER = 160       # px the ball artwork is rendered at (supersampled)


# ----------------------------------------------------------------------
# single instance
# ----------------------------------------------------------------------
_mutex_handle = None


def acquire_single_instance() -> bool:
    """True if this process is the one instance. On Windows, creates
    the named mutex; a second launch gets False. The handle is kept in
    a module global on purpose: if it were garbage-collected the lock
    would silently drop."""
    global _mutex_handle
    if not WIN:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        h = create(None, False, MUTEX_NAME)
        if not h:
            return True  # could not create the lock: do not kill the app
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(h)
            return False
        _mutex_handle = h
        return True
    except Exception:
        return True


def signal_existing_instance() -> None:
    """Ask the running instance to show its panel, then give up."""
    try:
        with socket.create_connection(("127.0.0.1", WAKE_PORT), timeout=1.5) as s:
            s.sendall(WAKE_MAGIC)
    except OSError:
        pass


def start_wake_listener(on_show) -> socket.socket | None:
    """Listen for wake-up pings from second launches. Returns the
    bound socket (close it on shutdown) or None if the port is taken,
    in which case the app simply runs without wake-up support."""
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", WAKE_PORT))
        srv.listen(4)
    except OSError:
        return None

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # socket closed during shutdown
            with conn:
                try:
                    data = conn.recv(64)
                except OSError:
                    data = b""
                if data.startswith(WAKE_MAGIC):
                    on_show()

    threading.Thread(target=loop, daemon=True, name="wake-listener").start()
    return srv


# ----------------------------------------------------------------------
# ball artwork
# ----------------------------------------------------------------------
def _load_glyph_font(px: int):
    """Return (font, char, embedded_color) for the ball's glyph:
    the sloth emoji in Segoe UI Emoji if possible, else a bold CJK
    fallback character. Best effort - never raises."""
    from PIL import ImageFont
    if WIN:
        try:
            return (ImageFont.truetype(r"C:\Windows\Fonts\seguiemj.ttf", px),
                    "\U0001F9A5", True)  # 🦥
        except Exception:
            pass
    for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
                 "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, px), "懒", False
        except Exception:
            continue
    from PIL import ImageFont as _F
    return _F.load_default(), "懒", False


def render_ball_image(px: int = BALL_RENDER):
    """Render the ball as an RGBA image: copper disc, soft drop
    shadow baked in, glyph centred. Drawn at 2x and let the OS scale
    it into the window so it stays crisp on high-DPI screens."""
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = px // 2
    r = int(px * 0.40)
    # soft shadow: blurred dark disc, nudged down
    shadow = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ds = ImageDraw.Draw(shadow)
    ds.ellipse([cx - r, cy - r + int(px * 0.05), cx + r, cy + r + int(px * 0.05)],
               fill=(10, 8, 4, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(px * 0.045))
    img.alpha_composite(shadow)
    # copper disc + a lighter cap for a hint of volume
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(180, 106, 43, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy - int(r * 0.25)],
              fill=(196, 122, 52, 255))
    # glyph
    gpx = int(px * 0.52)
    font, ch, color = _load_glyph_font(gpx)
    try:
        box = d.textbbox((0, 0), ch, font=font, embedded_color=color)
        w, h = box[2] - box[0], box[3] - box[1]
        d.text((cx - w / 2 - box[0], cy - h / 2 - box[1]), ch, font=font,
               embedded_color=color,
               fill=None if color else (248, 246, 240, 255))
    except Exception:
        pass
    return img


def _premultiplied_bgra(img) -> bytes:
    """UpdateLayeredWindow wants 32-bit BGRA with premultiplied
    alpha. Pillow images are straight RGBA; convert once at startup
    (160x160 pixels - trivial cost)."""
    rgba = img.convert("RGBA").tobytes()
    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        out[i] = b * a // 255
        out[i + 1] = g * a // 255
        out[i + 2] = r * a // 255
        out[i + 3] = a
    return bytes(out)


# ----------------------------------------------------------------------
# native floating ball (Win32 layered window, ctypes only)
# ----------------------------------------------------------------------
if WIN:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32w = ctypes.WinDLL("kernel32", use_last_error=True)

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HCURSOR), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    _WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)

    _WS_POPUP = 0x80000000
    _WS_EX_LAYERED = 0x00080000
    _WS_EX_TOPMOST = 0x00000008
    _WS_EX_TOOLWINDOW = 0x00000080
    _WM_DESTROY, _WM_CLOSE = 0x0002, 0x0010
    _WM_LBUTTONDOWN, _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_MOUSEMOVE = 0x0201, 0x0202, 0x0205, 0x0200
    _ULW_ALPHA, _AC_SRC_OVER, _AC_SRC_ALPHA = 2, 0, 1

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BallWindow:
    """The floating ball as a native layered window on its own
    message-loop thread. Click -> controller.toggle_panel();
    drag -> moves (new position reported via on_moved);
    right-click -> controller.quit(). No-op shell off Windows."""

    def __init__(self, controller, x: int, y: int, on_moved=None):
        self.controller = controller
        self.x, self.y = x, y
        self.on_moved = on_moved
        self.hwnd = None
        self._thread: threading.Thread | None = None
        self._wndproc = None  # keep the ctypes callback alive
        self._down_at: tuple[int, int, int, int] | None = None  # cx, cy, wx, wy
        self._dragging = False

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        if not WIN:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ball-window")
        self._thread.start()

    def close(self) -> None:
        if WIN and self.hwnd:
            _user32.PostMessageW(self.hwnd, _WM_CLOSE, 0, 0)

    # -- internals (ball thread only) ---------------------------------
    def _run(self) -> None:
        user32, gdi32 = _user32, _gdi32
        hinst = _kernel32w.GetModuleHandleW(None)
        cls_name = "LazyScholarBallWindow"
        self._wndproc = _WNDPROC(self._proc)
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW,
            cls_name, None, _WS_POPUP,
            self.x, self.y, BALL_SIZE, BALL_SIZE, None, None, hinst, None)
        if not hwnd:
            return
        self.hwnd = hwnd
        self._paint(hwnd)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _paint(self, hwnd) -> None:
        img = render_ball_image()
        px = img.width
        data = _premultiplied_bgra(img)
        user32, gdi32 = _user32, _gdi32
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = px, -px  # top-down DIB
        bmi.biPlanes, bmi.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem_dc, hbmp)
        pt = wintypes.POINT(self.x, self.y)
        size = wintypes.SIZE(BALL_SIZE, BALL_SIZE)
        src = wintypes.POINT(0, 0)
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(hwnd, screen_dc, ctypes.byref(pt),
                                   ctypes.byref(size), mem_dc, ctypes.byref(src),
                                   0, ctypes.byref(blend), _ULW_ALPHA)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    def _proc(self, hwnd, msg, wparam, lparam):
        user32 = _user32
        if msg == _WM_LBUTTONDOWN:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._down_at = (pt.x, pt.y, rect.left, rect.top)
            self._dragging = False
            return 0
        if msg == _WM_MOUSEMOVE and self._down_at is not None:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            cx, cy, wx, wy = self._down_at
            dx, dy = pt.x - cx, pt.y - cy
            if abs(dx) + abs(dy) > 6:
                self._dragging = True
            if self._dragging:
                user32.SetWindowPos(hwnd, None, wx + dx, wy + dy, 0, 0, 0x0001)
            return 0
        if msg == _WM_LBUTTONUP:
            was_drag = self._dragging
            self._down_at, self._dragging = None, False
            if was_drag:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self.x, self.y = rect.left, rect.top
                if self.on_moved:
                    self.on_moved(rect.left, rect.top)
            else:
                self.controller.toggle_panel()
            return 0
        if msg == _WM_RBUTTONUP:
            self.controller.quit()
            return 0
        if msg == _WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ----------------------------------------------------------------------
# panel effects
# ----------------------------------------------------------------------
def work_area() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary work area in
    physical pixels (ctypes metrics, consistent with pywebview's own
    coordinate space on the target machine)."""
    if WIN:
        rect = wintypes.RECT()
        if _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
        return (0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))
    return (0, 0, 1920, 1080)


def apply_rounded_corners(window) -> None:
    """Ask DWM for Win11 rounded corners on a pywebview window.
    Best effort: a hint to the OS, silently ignored where refused.
    Never use window regions or per-pixel alpha on the panel -
    either would permanently disqualify it from system rounding."""
    if not WIN or window is None:
        return
    try:
        hwnd = int(window.native.Handle.ToInt32())
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
    except Exception:
        pass


# ----------------------------------------------------------------------
# tray
# ----------------------------------------------------------------------
def build_tray(controller, icon_path: str | None):
    """Create the pystray icon and detach it. Returns the Icon or
    None when pystray is unavailable (the app still works: the ball
    carries show/quit on its own)."""
    if not WIN:
        return None
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    try:
        image = None
        if icon_path and Path(icon_path).exists():
            image = Image.open(icon_path)
        if image is None:
            image = render_ball_image(128)
        menu = pystray.Menu(
            pystray.MenuItem("打开面板", lambda icon, item: controller.toggle_panel(),
                             default=True),
            pystray.MenuItem("立即处理队列", lambda icon, item: controller.run_queue_now()),
            pystray.MenuItem("打开知识库文件夹", lambda icon, item: controller.open_vault()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 LazyScholar", lambda icon, item: controller.quit()),
        )
        icon = pystray.Icon("lazyscholar", image, "LazyScholar 懒人学者", menu)
        icon.run_detached()
        return icon
    except Exception:
        return None


# ----------------------------------------------------------------------
# controller
# ----------------------------------------------------------------------
class AppController:
    """Single owner of panel visibility and process exit.

    Every UI surface (tray, ball, JS bridge, wake listener) posts
    commands; only the controller's own thread touches windows. That
    thread is the one pywebview gives to webview.start(func=...).
    """

    def __init__(self, panel, panel_geo: tuple[int, int, int],
                 run_queue_now=None, open_vault=None):
        self.panel = panel
        self.panel_x, self.panel_y, self.hidden_x = panel_geo
        self._run_queue_now = run_queue_now
        self._open_vault = open_vault
        self.tray = None
        self.ball: BallWindow | None = None
        self.q: "queue.Queue[str]" = _make_queue()
        self.panel_visible = False
        self._gen = 0  # animation generation: stale animators self-abort
        self.quitting = False

    # -- thread-safe command posts ------------------------------------
    def toggle_panel(self) -> None:
        self.q.put("toggle")

    def show_panel(self) -> None:
        self.q.put("show")

    def hide_panel(self) -> None:
        self.q.put("hide")

    def run_queue_now(self) -> None:
        self.q.put("run_now")

    def open_vault(self) -> None:
        self.q.put("open_vault")

    def quit(self) -> None:
        self.q.put("quit")

    # -- command loop (runs on pywebview's func thread) ----------------
    def run(self) -> None:
        while True:
            cmd = self.q.get()
            if cmd == "quit":
                self._do_quit()
                return
            if self.quitting:
                continue
            try:
                if cmd == "toggle":
                    self._set_panel(not self.panel_visible)
                elif cmd == "show":
                    self._set_panel(True)
                elif cmd == "hide":
                    self._set_panel(False)
                elif cmd == "run_now" and self._run_queue_now:
                    threading.Thread(target=self._run_queue_now,
                                     daemon=True, name="queue-run").start()
                elif cmd == "open_vault" and self._open_vault:
                    self._open_vault()
            except Exception:
                pass  # a failed window op must never kill the loop

    # -- panel show/hide with slide animation --------------------------
    def _set_panel(self, visible: bool) -> None:
        if self.panel is None or visible == self.panel_visible:
            return
        self.panel_visible = visible
        self._gen += 1
        gen = self._gen
        win = self.panel
        if visible:
            win.move(self.hidden_x, self.panel_y)
            win.show()
            apply_rounded_corners(win)
            self._slide(win, self.hidden_x, self.panel_x, gen)
        else:
            self._slide(win, self.panel_x, self.hidden_x, gen)
            if gen == self._gen:
                win.hide()

    def _slide(self, win, x_from: int, x_to: int, gen: int, dur: float = 0.18) -> None:
        steps = max(8, int(dur / 0.016))
        for i in range(1, steps + 1):
            if gen != self._gen or self.quitting:
                return
            t = i / steps
            ease = 1 - (1 - t) ** 3
            win.move(int(x_from + (x_to - x_from) * ease), self.panel_y)
            time.sleep(0.016)

    # -- exit -----------------------------------------------------------
    def _do_quit(self) -> None:
        self.quitting = True
        self._gen += 1
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            if self.ball is not None:
                self.ball.close()
        except Exception:
            pass
        try:
            if self.panel is not None:
                self.panel.destroy()
        except Exception:
            pass


def _make_queue():
    import queue as _q
    return _q.Queue()
