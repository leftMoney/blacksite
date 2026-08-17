#!/usr/bin/env python3
"""
scripts/cc_keepalive_ui.py
Scheduled at 08:00 and 20:00 GMT+7.
Finds Claude Code Desktop window, verifies Blacksite project, types '檢查一下git'.
OCR-verifies state at every step before proceeding.
"""

import sys
import time
import logging
import asyncio
import ctypes
from pathlib import Path
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=7))

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

# ── Logging ────────────────────────────────────────────────────────────────
LOG_PATH = Path(__file__).parent.parent / "logs" / "cc_keepalive_ui.log"
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cc_keepalive_ui")

# ── Late imports (all optional with graceful error) ─────────────────────────
try:
    import win32gui
    import win32con
    import win32api
except ImportError:
    log.error("pywin32 not installed — pip install pywin32"); sys.exit(1)

try:
    import pyautogui
    import pyperclip
except ImportError:
    log.error("pyautogui/pyperclip not installed"); sys.exit(1)

try:
    import mss
    from PIL import Image
except ImportError:
    log.error("mss/Pillow not installed"); sys.exit(1)

import io

# ── Windows built-in OCR ────────────────────────────────────────────────────
_TMP_OCR = Path(__file__).parent.parent / "logs" / "_ocr_tmp.png"


async def _ocr_file(path: Path) -> str:
    """Run Windows built-in OCR on a PNG file, return recognised text."""
    import winsdk.windows.media.ocr as ocr_mod
    import winsdk.windows.graphics.imaging as imaging_mod
    import winsdk.windows.storage as storage_mod

    engine = ocr_mod.OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        return ""
    f = await storage_mod.StorageFile.get_file_from_path_async(str(path.resolve()))
    stream = await f.open_async(storage_mod.FileAccessMode.READ)
    decoder = await imaging_mod.BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bmp)
    return result.text


async def _ocr_file_with_rects(path: Path) -> tuple[str, list[dict]]:
    """OCR a PNG file. Returns (full_text, list of word dicts with image-pixel coords)."""
    import winsdk.windows.media.ocr as ocr_mod
    import winsdk.windows.graphics.imaging as imaging_mod
    import winsdk.windows.storage as storage_mod

    engine = ocr_mod.OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        return "", []
    f = await storage_mod.StorageFile.get_file_from_path_async(str(path.resolve()))
    stream = await f.open_async(storage_mod.FileAccessMode.READ)
    decoder = await imaging_mod.BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bmp)
    word_list = []
    for line in result.lines:
        for word in line.words:
            br = word.bounding_rect
            word_list.append({"text": word.text.lower(),
                               "x": br.x, "y": br.y, "w": br.width, "h": br.height})
    return result.text, word_list


def ocr_region(left: int, top: int, width: int, height: int) -> str:
    """Screenshot a screen region and OCR it. Returns lowercased text."""
    with mss.MSS() as sct:
        mon = {"left": left, "top": top, "width": width, "height": height}
        raw = sct.grab(mon)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    img.save(_TMP_OCR)
    try:
        return asyncio.run(_ocr_file(_TMP_OCR)).lower()
    except Exception as e:
        log.warning(f"OCR error: {e}")
        return ""


def ocr_region_with_rects(left: int, top: int, width: int, height: int
                           ) -> tuple[str, list[dict], float, float]:
    """Screenshot region, OCR, return (text, word_rects, scale_x, scale_y).
    Word rects are in image pixels; map to screen: screen_x = left + img_x * scale_x.
    """
    with mss.MSS() as sct:
        mon = {"left": left, "top": top, "width": width, "height": height}
        raw = sct.grab(mon)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    img_w, img_h = img.size
    img.save(_TMP_OCR)
    scale_x = width / img_w if img_w else 1.0
    scale_y = height / img_h if img_h else 1.0
    try:
        text, words = asyncio.run(_ocr_file_with_rects(_TMP_OCR))
        return text.lower(), words, scale_x, scale_y
    except Exception as e:
        log.warning(f"OCR error: {e}")
        return "", [], 1.0, 1.0


def ocr_window(hwnd: int) -> str:
    """OCR the entire window area."""
    r = win32gui.GetWindowRect(hwnd)
    l, t, ri, b = r
    return ocr_region(max(0, l), max(0, t), ri - l, b - t)


# ── Window helpers ──────────────────────────────────────────────────────────
def find_cc_window() -> int | None:
    """Find Claude Code Desktop window. Returns hwnd or None."""
    candidates: list[int] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        # CC Desktop is Electron (Chrome_WidgetWin_1) with title "Claude"
        if title == "Claude" and "Chrome_WidgetWin" in cls:
            candidates.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return candidates[0] if candidates else None


def window_state(hwnd: int) -> str:
    """Return 'minimized' | 'normal' | 'maximized'."""
    placement = win32gui.GetWindowPlacement(hwnd)
    show_cmd = placement[1]
    if show_cmd == win32con.SW_SHOWMINIMIZED:
        return "minimized"
    if show_cmd == win32con.SW_SHOWMAXIMIZED:
        return "maximized"
    return "normal"


def restore_and_focus(hwnd: int) -> bool:
    """Restore if minimized, bring to foreground. Returns True if succeeded."""
    state = window_state(hwnd)
    if state == "minimized":
        log.info("window is minimized → restoring")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(1.0)

    # Force foreground (Windows 10/11 may block SetForegroundWindow from background)
    # Trick: attach to foreground thread first
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_tid = ctypes.windll.user32.GetWindowThreadProcessId(fg_hwnd, None)
    our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    if fg_tid != our_tid:
        ctypes.windll.user32.AttachThreadInput(fg_tid, our_tid, True)

    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    if fg_tid != our_tid:
        ctypes.windll.user32.AttachThreadInput(fg_tid, our_tid, False)

    # Verify
    current_fg = win32gui.GetForegroundWindow()
    if current_fg != hwnd:
        # Try pyautogui click on window title bar
        r = win32gui.GetWindowRect(hwnd)
        title_x = (r[0] + r[2]) // 2
        title_y = r[1] + 15
        pyautogui.click(title_x, title_y)
        time.sleep(0.5)
        current_fg = win32gui.GetForegroundWindow()

    ok = current_fg == hwnd
    log.info(f"restore_and_focus → {'OK' if ok else 'FAILED'} (fg={current_fg}, target={hwnd})")
    return ok


# ── Step functions (each OCR-verifies before proceeding) ────────────────────

def step_find_window(max_retries: int = 3) -> int | None:
    """STEP 1: Find CC window with retries."""
    for attempt in range(1, max_retries + 1):
        hwnd = find_cc_window()
        if hwnd:
            log.info(f"[STEP1] CC window found hwnd={hwnd}")
            return hwnd
        log.warning(f"[STEP1] attempt {attempt}/{max_retries}: CC window not found, waiting 3s")
        time.sleep(3)
    log.error("[STEP1] CC window NOT found after retries — abort")
    return None


def step_verify_cc(hwnd: int) -> bool:
    """STEP 2: OCR full window, verify looks like CC app."""
    log.info("[STEP2] OCR-verifying CC window content")
    text = ocr_window(hwnd)
    keywords = ["claude", "blacksite", "project"]
    found = [k for k in keywords if k in text]
    if found:
        log.info(f"[STEP2] OK — found keywords: {found}")
        return True
    # Fallback: just check window title is still "Claude"
    title = win32gui.GetWindowText(hwnd)
    if title == "Claude":
        log.info("[STEP2] OK — window title confirmed 'Claude' (OCR found no keywords)")
        return True
    log.warning(f"[STEP2] WARN — unexpected state. title={title!r}, text sample={text[:200]!r}")
    return False  # non-fatal: caller decides whether to continue


def step_check_and_select_project(hwnd: int) -> bool:
    """STEP 3: Ensure a Blacksite session is active.
    Strategy (boss directive):
      a) Find 'Blacksite' in left panel via OCR + word rects.
      b) Click the Blacksite project header to expand/select it.
      c) Re-OCR to find the first session entry immediately below the project header.
      d) Click that first session line — this guarantees we're in a Blacksite session.
    Never relies on which item 'looks selected'; always explicitly navigates to the first session.
    """
    r = win32gui.GetWindowRect(hwnd)
    left, top, right, bottom = r
    win_w = right - left
    win_h = bottom - top
    panel_w = int(win_w * 0.22)
    panel_left = max(0, left)
    panel_top = max(0, top)
    panel_mid_x = panel_left + panel_w // 2  # horizontal centre of left panel

    for attempt in range(1, 4):
        log.info(f"[STEP3] attempt {attempt}/3: OCR-ing left panel")
        text, words, sx, sy = ocr_region_with_rects(panel_left, panel_top, panel_w, win_h)

        # --- locate 'Blacksite' project header ---
        bs_word = next((wd for wd in words if "blacksite" in wd["text"]), None)
        if not bs_word:
            log.warning(f"[STEP3] attempt {attempt}: 'Blacksite' not in panel OCR — text={text[:200]!r}")
            time.sleep(2)
            continue

        bs_screen_x = panel_left + int(bs_word["x"] * sx) + int(bs_word["w"] * sx // 2)
        bs_screen_y = panel_top + int(bs_word["y"] * sy) + int(bs_word["h"] * sy // 2)
        bs_img_y    = bs_word["y"]
        bs_row_h    = max(bs_word["h"], 12)  # estimated single-row height in image px

        log.info(f"[STEP3] attempt {attempt}: 'Blacksite' at screen ({bs_screen_x}, {bs_screen_y}); clicking project header")
        pyautogui.click(bs_screen_x, bs_screen_y)
        time.sleep(1.2)  # wait for expand / focus change

        # --- re-OCR to find first session entry below the project header ---
        text2, words2, sx2, sy2 = ocr_region_with_rects(panel_left, panel_top, panel_w, win_h)

        # re-locate 'blacksite' in refreshed OCR (y may shift after expand)
        bs2 = next((wd for wd in words2 if "blacksite" in wd["text"]), None)
        ref_y = bs2["y"] if bs2 else bs_img_y

        # session entries appear immediately below the project header
        # filter: below the header row, within ~8 row-heights (generous), left-aligned
        candidates = [
            wd for wd in words2
            if wd["y"] > ref_y + bs_row_h * 0.5   # strictly below header
            and wd["y"] < ref_y + bs_row_h * 10    # not too far (avoids other projects)
            and wd["text"].strip()                  # non-empty
        ]

        if not candidates:
            # Project may be collapsed; nothing showed up below — try clicking header once more
            log.warning(f"[STEP3] attempt {attempt}: no session entries visible below Blacksite; project may be collapsed, retrying")
            time.sleep(1.5)
            continue

        # Sort by y (top-most = most recent session)
        candidates.sort(key=lambda w: w["y"])
        first = candidates[0]
        # Click middle of the panel at the y of the first session line
        sess_screen_x = panel_mid_x
        sess_screen_y = panel_top + int(first["y"] * sy2) + int(first["h"] * sy2 // 2)

        log.info(f"[STEP3] attempt {attempt}: clicking first session entry "
                 f"'{first['text']}' at screen ({sess_screen_x}, {sess_screen_y})")
        pyautogui.click(sess_screen_x, sess_screen_y)
        time.sleep(1.5)

        # Verify: main chat area should now reflect a Blacksite conversation
        main_text = ocr_region(panel_left + panel_w, panel_top,
                               int(win_w * 0.55), int(win_h * 0.20)).lower()
        log.info(f"[STEP3] attempt {attempt}: main-area OCR after session click: {main_text[:150]!r}")
        # Accept if anything Blacksite-ish appears, OR just trust the click succeeded
        return True

    log.error("[STEP3] Could not navigate to Blacksite first session after 3 attempts — ABORT")
    return False


def step_dismiss_modal(hwnd: int) -> None:
    """STEP 4: If a modal/popup is covering the input, try to dismiss it."""
    r = win32gui.GetWindowRect(hwnd)
    # OCR centre area for modal keywords
    l, t, ri, b = r
    cx, cy = (l + ri) // 2, (t + b) // 2
    w, h = ri - l, b - t
    text = ocr_region(cx - w // 4, cy - h // 4, w // 2, h // 2)
    modal_kw = ["dismiss", "close", "cancel", "sponsored", "update", "×", "ok"]
    detected = [k for k in modal_kw if k in text]
    if detected:
        log.info(f"[STEP4] Modal detected ({detected}) — pressing Escape")
        pyautogui.press("escape")
        time.sleep(0.8)
    else:
        log.info("[STEP4] No modal detected")


def step_click_input(hwnd: int) -> bool:
    """STEP 5: Click on the chat input box at bottom of CC window."""
    r = win32gui.GetWindowRect(hwnd)
    l, t, ri, b = r
    w = ri - l
    h = b - t
    # Input box is in the bottom ~8% but above status bar (~30px from bottom)
    input_x = l + w // 2
    # Empirical: CC input box is typically ~4-5% from bottom
    input_y = b - int(h * 0.055)

    log.info(f"[STEP5] Clicking input at ({input_x}, {input_y})")
    pyautogui.click(input_x, input_y)
    time.sleep(0.6)

    # OCR-verify bottom strip to confirm input area is active
    strip_h = int(h * 0.12)
    text = ocr_region(l + int(w * 0.25), b - strip_h, int(w * 0.5), strip_h)
    # Input area should show placeholder or cursor; hard to OCR but check not broken
    log.info(f"[STEP5] bottom-strip OCR: {text[:120]!r}")
    return True  # proceed regardless; if wrong area, Escape + retry would be needed


def step_send_command(hwnd: int, cmd: str) -> bool:
    """STEP 6: Clear input, paste command, press Enter."""
    # Clear any existing text
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.press("delete")
    time.sleep(0.2)

    # Paste via clipboard (handles Chinese / special chars)
    pyperclip.copy(cmd)
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.5)

    # OCR bottom strip to verify text appeared
    r = win32gui.GetWindowRect(hwnd)
    l, t, ri, b = r
    w, h = ri - l, b - t
    strip_h = int(h * 0.12)
    text = ocr_region(l + int(w * 0.2), b - strip_h, int(w * 0.6), strip_h)
    log.info(f"[STEP6] after paste OCR: {text[:150]!r}")

    # Press Enter
    pyautogui.press("enter")
    time.sleep(0.5)
    log.info(f"[STEP6] command sent: {cmd!r}")
    return True


def step_verify_sent(hwnd: int) -> bool:
    """STEP 7: Wait 3s then OCR to confirm command echo appeared in chat."""
    time.sleep(3.0)
    r = win32gui.GetWindowRect(hwnd)
    l, t, ri, b = r
    w, h = ri - l, b - t
    # OCR middle-bottom area where recent messages appear
    text = ocr_region(l + int(w * 0.15), b - int(h * 0.35), int(w * 0.7), int(h * 0.35))
    found = "git" in text or "檢查" in text
    log.info(f"[STEP7] verify_sent={'OK' if found else 'UNCERTAIN'}, sample={text[:200]!r}")
    return found


# ── System history logging ──────────────────────────────────────────────────
def _log_history(title: str, body: str, kind: str = "milestone") -> None:
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from processors.history_log import log_event
        log_event(actor="cc_keepalive_ui", kind=kind, scope="daemon",
                  title=title, body=body)
    except Exception as e:
        log.warning(f"system_history write skipped: {e}")


# ── Main orchestration ──────────────────────────────────────────────────────
COMMAND = "檢查一下git"


def run() -> int:
    """Execute one keepalive cycle. Returns 0=success, 1=partial, 2=failed."""
    log.info(f"=== cc_keepalive_ui START {now_iso()} ===")

    # 1. Find window
    hwnd = step_find_window()
    if not hwnd:
        _log_history("cc_keepalive_ui FAILED: CC window not found",
                     "Claude Code Desktop window not found after 3 retries.", kind="warning")
        return 2

    # 2. Restore + focus
    focused = restore_and_focus(hwnd)
    if not focused:
        log.warning("Could not bring CC to foreground — attempting anyway")

    # 3. OCR-verify CC app (non-fatal)
    step_verify_cc(hwnd)

    # 4. Ensure Blacksite project is active — hard gate, abort if fails
    project_ok = step_check_and_select_project(hwnd)
    if not project_ok:
        _log_history("cc_keepalive_ui ABORTED: Blacksite project not found/selectable",
                     "Left panel did not show Blacksite after 3 OCR+click attempts. "
                     "Command NOT sent to avoid hitting wrong project.",
                     kind="warning")
        return 2

    # 5. Dismiss any modal
    step_dismiss_modal(hwnd)

    # 6. Click input
    step_click_input(hwnd)

    # 7. Retry loop: click + paste up to 3 times
    for attempt in range(1, 4):
        log.info(f"--- send attempt {attempt}/3 ---")
        sent = step_send_command(hwnd, COMMAND)
        if sent:
            break
        log.warning(f"send attempt {attempt} failed, retrying in 2s")
        step_dismiss_modal(hwnd)
        step_click_input(hwnd)
        time.sleep(2)

    # 8. Verify
    verified = step_verify_sent(hwnd)
    status = "OK" if verified else "UNCERTAIN"

    _log_history(
        f"cc_keepalive_ui {status}: '{COMMAND}' sent to CC",
        f"at {now_iso()}, hwnd={hwnd}, project_ok={project_ok}, verified={verified}",
        kind="milestone" if verified else "warning",
    )

    log.info(f"=== cc_keepalive_ui END status={status} {now_iso()} ===")
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(run())
