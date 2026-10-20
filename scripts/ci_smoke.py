"""CI smoke test + visual QA capture (runs on the Windows runner).

Builds evidence the maintainer agent can actually look at:
- qa/ball-render.png : the ball artwork rendered by app_shell itself
- qa/state.json      : /api/state from the launched exe
- qa/shot-ball.png   : desktop screenshot after launch (ball + tray)
- qa/shot-panel.png  : desktop screenshot after waking the panel

Screenshots may come back black on a session-0 runner; that is
recorded in qa/smoke-log.txt instead of failing the build. The hard
gates are: exe exists, /api/state answers, /app serves 200.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "qa"
QA.mkdir(exist_ok=True)
LOG = []


def note(msg: str) -> None:
    LOG.append(msg)
    print(msg, flush=True)


def screenshot(path: Path) -> None:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        f"$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
        f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   timeout=60, check=False)
    note(f"screenshot -> {path.name} exists={path.exists()}")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    # 1. Ball self-render (pure Pillow: always works, verifies artwork
    #    and the DPI size math on this machine).
    try:
        import app_shell
        px = app_shell.ball_pixel_size()
        img = app_shell.render_ball_image(px)
        img.save(QA / "ball-render.png")
        note(f"ball render ok: {px}px at this runner's scaling")
    except Exception as e:
        note(f"ball render FAILED: {e}")

    exe = ROOT / "dist" / "LazyScholar.exe"
    if not exe.exists():
        note("exe missing - build step must have failed")
        (QA / "smoke-log.txt").write_text("\n".join(LOG), encoding="utf-8")
        return 1

    vault = ROOT / "ci-vault"
    env = dict(os.environ, LAZYSCHOLAR_VAULT=str(vault))
    proc = subprocess.Popen([str(exe)], env=env)
    note(f"exe launched pid={proc.pid}")
    ok = False
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:8765/api/state", timeout=3) as r:
                    state = r.read().decode("utf-8", "replace")
                (QA / "state.json").write_text(state, encoding="utf-8")
                with urllib.request.urlopen(
                        "http://127.0.0.1:8765/app", timeout=3) as r:
                    note(f"/app -> {r.status}")
                ok = True
                break
            except Exception:
                if proc.poll() is not None:
                    note(f"exe exited early rc={proc.returncode}")
                    break
                time.sleep(3)
        note(f"api state ok={ok}")
        if ok:
            time.sleep(3)
            screenshot(QA / "shot-ball.png")
            try:
                with socket.create_connection(("127.0.0.1", 47811), timeout=3) as s:
                    s.sendall(b"lazyscholar:show")
                note("wake signal sent")
            except OSError as e:
                note(f"wake failed: {e}")
            time.sleep(3)
            screenshot(QA / "shot-panel.png")
    finally:
        subprocess.run(["taskkill", "/IM", "LazyScholar.exe", "/F"],
                       check=False, capture_output=True)
        (QA / "smoke-log.txt").write_text("\n".join(LOG), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
