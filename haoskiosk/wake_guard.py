#!/usr/bin/env python3
"""-------------------------------------------------------------------------------
# HAOS Kiosk Display — Wake Tap Guard
# File: wake_guard.py
#
#### DESCRIPTION:
   Prevents the tap that wakes a blanked (DPMS) screen from also "clicking"
   whatever is underneath it in the browser.

   Polls the X server's DPMS state. When the monitor leaves the "On" state, a
   transparent-to-the-user (black) fullscreen override-redirect window is
   mapped and the pointer is grabbed by it. The next touch/click therefore
   goes to this window (and wakes the screen via DPMS) rather than the
   browser. The window unmaps itself when that touch is released, or if the
   screen was woken some other way (key press, REST call) without a touch.

#### USAGE:
   wake_guard.py [-d|--debug]
-------------------------------------------------------------------------------"""
import sys
import time

from Xlib import X, display

POLL_IDLE = 0.25         # Seconds between DPMS checks while screen is on
POLL_ACTIVE = 0.02       # Seconds between event checks while overlay is up
NO_TOUCH_GRACE = 0.5     # Close overlay this long after screen wakes w/o a touch
MAX_PRESS = 3.0          # Safety: close if release never arrives
DEBUG = "-d" in sys.argv or "--debug" in sys.argv


def log(msg):
    if DEBUG:
        print(f"[wake_guard] {msg}", flush=True)


def dpms_on(d):
    """True if monitor is fully on (or DPMS is unavailable/disabled)."""
    try:
        info = d.dpms_info()
        if not info.state:  # DPMS disabled
            return True
        return info.power_level == 0  # 0 == DPMSModeOn
    except Exception:
        return True


def create_overlay(d):
    root = d.screen().root
    geo = root.get_geometry()
    black = d.screen().black_pixel
    win = root.create_window(
        0, 0, geo.width, geo.height, 0, d.screen().root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=black, override_redirect=True,
        event_mask=X.ButtonPressMask | X.ButtonReleaseMask)
    return win


def show_overlay(d, win):
    win.configure(stack_mode=X.Above)
    win.map()
    d.sync()
    for _ in range(50):  # Grab can fail briefly if another client holds a grab
        res = win.grab_pointer(False, X.ButtonPressMask | X.ButtonReleaseMask,
                               X.GrabModeAsync, X.GrabModeAsync, win, X.NONE, X.CurrentTime)
        if res == X.GrabSuccess:
            return
        time.sleep(0.02)
    log("pointer grab failed (overlay still maps and catches events over it)")


def hide_overlay(d, win):
    d.ungrab_pointer(X.CurrentTime)
    win.unmap()
    d.sync()


def main():
    d = display.Display()
    if not d.has_extension("DPMS"):
        log("DPMS extension unavailable; exiting")
        return 1
    win = create_overlay(d)
    while True:
        if dpms_on(d):
            time.sleep(POLL_IDLE)
            continue

        log("screen blanked: showing overlay")
        show_overlay(d, win)
        pressed_at = None
        woke_at = None
        while True:
            time.sleep(POLL_ACTIVE)
            now = time.monotonic()
            done = False
            while d.pending_events():
                ev = d.next_event()
                if ev.type == X.ButtonPress and pressed_at is None:
                    pressed_at = now
                    log("press swallowed")
                elif ev.type == X.ButtonRelease and pressed_at is not None:
                    done = True
                    log("release: closing overlay")
            if pressed_at is not None and now - pressed_at > MAX_PRESS:
                done = True
            if dpms_on(d):
                woke_at = woke_at or now
                if pressed_at is None and now - woke_at > NO_TOUCH_GRACE:
                    log("woken without touch: closing overlay")
                    done = True
            else:
                woke_at = None
            if done:
                hide_overlay(d, win)
                break


if __name__ == "__main__":
    sys.exit(main())
