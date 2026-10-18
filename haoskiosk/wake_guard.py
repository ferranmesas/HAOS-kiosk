#!/usr/bin/env python3
"""-------------------------------------------------------------------------------
# HAOS Kiosk Display — Wake Tap Guard
# File: wake_guard.py
#
#### DESCRIPTION:
   Prevents the tap that wakes a blanked (DPMS) screen from also "clicking"
   whatever is underneath it in the browser.

   When the monitor leaves the DPMS "On" state, a black fullscreen
   override-redirect window is mapped and the pointer is grabbed by it. The
   next touch/click therefore goes to this window (and wakes the screen via
   DPMS) rather than the browser. The window unmaps itself when that touch is
   released, or if the screen was woken some other way (key press, REST call)
   without a touch.

   DPMS state changes are learned from the DPMS 1.2 'DPMSInfoNotify' generic
   event (requested with DPMSSelectInput), so no polling is needed. python-xlib
   does not wrap that part of the extension, so the request and event layout
   (from X11/extensions/dpmsproto.h) are defined here. If the server does not
   support DPMS >= 1.2 (or the Generic Event Extension), the script falls back
   to polling the DPMS state every POLL_FALLBACK seconds.

#### USAGE:
   wake_guard.py [-d|--debug]
-------------------------------------------------------------------------------"""
import logging
import select
import sys
import time

from Xlib import X, display, error
from Xlib.ext import ge
from Xlib.protocol import rq

DPMS_EXT = "DPMS"
DPMS_INFO_NOTIFY = 0         # evtype of DPMSInfoNotify
DPMS_INFO_NOTIFY_MASK = 1    # 1 << 0
DPMS_MODE_ON = 0
DPMS_MODES = {0: "On", 1: "Standby", 2: "Suspend", 3: "Off"}

POLL_FALLBACK = 2.0          # Seconds between DPMS polls if events unavailable
RESYNC = 30.0                # Seconds between sanity re-reads of DPMS state when using events
NO_TOUCH_GRACE = 0.5         # Close overlay this long after screen wakes w/o a touch
MAX_PRESS = 3.0              # Safety: close if release never arrives

log = logging.getLogger("wake_guard")


#### DPMS 1.2 event support (not provided by python-xlib)
class DPMSSelectInput(rq.Request):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(8),
        rq.RequestLength(),
        rq.Card32("event_mask"),
    )


# Layout of the generic event payload after the 10-byte generic event header
DPMS_INFO_NOTIFY_DATA = rq.Struct(
    rq.Pad(2),
    rq.Card32("timestamp"),
    rq.Card16("power_level"),
    rq.Bool("state"),
    rq.Pad(13),
)


def level_name(level):
    return DPMS_MODES.get(level, f"?{level}")


def is_blanked(power_level, enabled):
    return bool(enabled) and power_level != DPMS_MODE_ON


def read_blanked(d):
    """Query current DPMS state. Returns (blanked, power_level, enabled)."""
    info = d.dpms_info()
    return is_blanked(info.power_level, info.state), info.power_level, info.state


def enable_dpms_events(d):
    """Subscribe to DPMSInfoNotify. Returns DPMS major opcode, or None if unsupported."""
    if not d.has_extension(DPMS_EXT):
        log.warning("DPMS extension not available")
        return None
    ver = d.dpms_get_version()
    log.info("DPMS version %d.%d", ver.major_version, ver.minor_version)
    if (ver.major_version, ver.minor_version) < (1, 2):
        log.warning("DPMS < 1.2: no DPMSInfoNotify event support")
        return None
    if not d.has_extension("Generic Event Extension"):
        log.warning("Generic Event Extension not available")
        return None

    opcode = d.get_extension_major(DPMS_EXT)
    d.ge_add_event_data(opcode, DPMS_INFO_NOTIFY, DPMS_INFO_NOTIFY_DATA)
    errors = []
    DPMSSelectInput(display=d.display, onerror=lambda err, req: errors.append(err),
                    opcode=opcode, event_mask=DPMS_INFO_NOTIFY_MASK)
    d.sync()
    if errors:
        log.warning("DPMSSelectInput rejected by server: %s", errors[0])
        return None
    return opcode


def dpms_event(ev, opcode):
    """If 'ev' is a DPMSInfoNotify, return (blanked, power_level, enabled); else None."""
    if getattr(ev, "type", None) != ge.GenericEventCode:
        return None
    if getattr(ev, "extension", None) != opcode or getattr(ev, "evtype", None) != DPMS_INFO_NOTIFY:
        return None
    data = ev.data
    return is_blanked(data.power_level, data.state), data.power_level, data.state


#### Overlay window
def create_overlay(d):
    screen = d.screen()
    geo = screen.root.get_geometry()
    log.info("Overlay size: %dx%d", geo.width, geo.height)
    return screen.root.create_window(
        0, 0, geo.width, geo.height, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.black_pixel, override_redirect=True,
        event_mask=X.ButtonPressMask | X.ButtonReleaseMask)


def show_overlay(d, win):
    win.configure(stack_mode=X.Above)
    win.map()
    d.sync()
    for attempt in range(50):  # Grab can fail briefly if another client holds a grab
        res = win.grab_pointer(False, X.ButtonPressMask | X.ButtonReleaseMask,
                               X.GrabModeAsync, X.GrabModeAsync, win, X.NONE, X.CurrentTime)
        if res == X.GrabSuccess:
            log.debug("Pointer grabbed (attempt %d)", attempt + 1)
            return
        time.sleep(0.02)
    log.warning("Pointer grab failed (status %s); relying on overlay window alone", res)


def hide_overlay(d, win):
    d.ungrab_pointer(X.CurrentTime)
    win.unmap()
    d.sync()


#### Main loop
def run(d, opcode):
    win = create_overlay(d)
    use_events = opcode is not None
    if use_events:
        log.info("Using DPMSInfoNotify events (resync every %.0fs)", RESYNC)
    else:
        log.info("Event support unavailable: polling DPMS every %.1fs", POLL_FALLBACK)

    blanked, level, enabled = read_blanked(d)
    log.info("Initial DPMS state: %s (DPMS %s)", level_name(level), "enabled" if enabled else "disabled")

    shown = False
    blanked_at = None   # monotonic time overlay was shown
    pressed_at = None   # monotonic time of swallowed press
    woke_at = None      # monotonic time screen reported on while overlay up
    next_check = time.monotonic() + (RESYNC if use_events else POLL_FALLBACK)

    def set_state(new_blanked, new_level, source):
        nonlocal blanked
        if source == "event" or new_blanked != blanked:
            log.debug("DPMS %s: %s", source, level_name(new_level))
        blanked = new_blanked

    def close_overlay():
        nonlocal shown, pressed_at, woke_at
        hide_overlay(d, win)
        shown, pressed_at, woke_at = False, None, None

    while True:
        # 1. Handle everything queued
        while d.pending_events():
            ev = d.next_event()
            now = time.monotonic()
            parsed = dpms_event(ev, opcode) if use_events else None
            if parsed:
                set_state(parsed[0], parsed[1], "event")
            elif ev.type == X.ButtonPress and shown and pressed_at is None:
                pressed_at = now
                log.info("Swallowed press at (%d,%d), %.2fs after blank", ev.root_x, ev.root_y, now - blanked_at)
            elif ev.type == X.ButtonRelease and shown and pressed_at is not None:
                log.info("Release after %.0fms: closing overlay", (now - pressed_at) * 1000)
                close_overlay()
            else:
                log.debug("Ignored event: %s", ev)

        # 2. Periodic poll (fallback) or resync (event mode)
        now = time.monotonic()
        if now >= next_check:
            polled, level, _ = read_blanked(d)
            if use_events and polled != blanked:
                log.warning("Missed DPMS event: tracked %s, server reports %s",
                            "blanked" if blanked else "on", level_name(level))
            set_state(polled, level, "poll")
            next_check = now + (RESYNC if use_events else POLL_FALLBACK)

        # 3. Act on state
        if not shown and blanked:
            log.info("Screen blanked: showing overlay")
            show_overlay(d, win)
            shown, blanked_at, pressed_at, woke_at = True, now, None, None
        elif shown:
            woke_at = None if blanked else (woke_at or now)
            if pressed_at is not None and now - pressed_at > MAX_PRESS:
                log.warning("No release within %.0fs: closing overlay", MAX_PRESS)
                close_overlay()
            elif woke_at is not None and pressed_at is None and now - woke_at > NO_TOUCH_GRACE:
                log.info("Screen woke without a touch (after %.0fs blank): closing overlay", now - blanked_at)
                close_overlay()

        # 4. Sleep until next X event or the nearest deadline
        deadlines = [next_check]
        if pressed_at is not None:
            deadlines.append(pressed_at + MAX_PRESS + 0.01)
        elif woke_at is not None:
            deadlines.append(woke_at + NO_TOUCH_GRACE + 0.01)
        if not d.pending_events():
            select.select([d.fileno()], [], [], max(0.0, min(deadlines) - time.monotonic()))


def main():
    debug = "-d" in sys.argv or "--debug" in sys.argv
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="[wake_guard] %(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    d = display.Display()
    if not d.has_extension(DPMS_EXT):
        log.error("DPMS extension unavailable; exiting")
        return 1
    try:
        opcode = enable_dpms_events(d)
    except (error.XError, AttributeError) as exc:  # Defensive: never die over the optimization
        log.warning("Could not enable DPMS events (%s); using polling", exc)
        opcode = None
    run(d, opcode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
