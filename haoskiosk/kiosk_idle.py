#!/usr/bin/env python3
"""
HAOS Kiosk: Idle/first-touch swallow daemon (overlay method)

Goal:
  After SCREEN_TIMEOUT seconds of *user inactivity*, blank the display (xset dpms force off)
  and install a fullscreen overlay window that captures the *first* touch/click.
  That first interaction wakes the screen (xset dpms force on) but is swallowed so
  it does NOT reach the browser/dashboard. Overlay is then removed and normal
  interaction resumes.

Design (no periodic polling):
  We emulate an event-driven wait using select() on the X connection with a timeout
  equal to remaining idle interval. If no events arrive before timeout -> blank.
  While blanked, we block in select() until an input event arrives on the overlay.

Assumptions / Simplifications:
  - SCREEN_TIMEOUT > 0 (caller ensures daemon not started if 0 desired behavior).
  - Touch events are translated to core pointer ButtonPress/Release (common for libinput).
  - We ignore manual display_on/off or rotation changes (as per user request).
  - We disable built-in DPMS timers (run.sh does xset -dpms; xset s off before starting us).
  - We do not attempt to re-arm if an external agent changes DPMS state.

Edge Cases:
  - Rapid double tap: Only first press swallowed; second proceeds.
  - Long press: Entire first button press+release swallowed.
  - Multitouch generating multiple ButtonPress events rapidly: We treat first event as swallow trigger and immediately wake; subsequent events after overlay removal will reach browser (acceptable compromise).

Failures gracefully logged to stdout.
"""
import os
import sys
import time
import select
import subprocess
import logging
from typing import Optional

# Configure logging early
LOG_LEVEL = logging.DEBUG if os.getenv("KIOSK_IDLE_DEBUG", "false").lower() == "true" else logging.INFO
logging.basicConfig(stream=sys.stdout, level=LOG_LEVEL,
                    format='[%(asctime)s] %(levelname)s: [kiosk_idle] %(message)s', datefmt='%H:%M:%S', force=True)

try:
    from Xlib import X, display
    from Xlib.protocol import request
    from Xlib.ext import screensaver  # Added for accurate idle detection
    from Xlib.ext import xinput  # For Raw (XI2) events fallback
except Exception as e:  # pragma: no cover
    logging.error(f"python-xlib not available or failed to import: {e}")
    sys.exit(1)

SCREEN_TIMEOUT_ENV = os.getenv("SCREEN_TIMEOUT", "0")
SWALLOW_FIRST_TOUCH = os.getenv("SWALLOW_FIRST_TOUCH", "true").lower() == "true"

try:
    SCREEN_TIMEOUT = int(float(SCREEN_TIMEOUT_ENV))
except ValueError:
    logging.error(f"Invalid SCREEN_TIMEOUT value: {SCREEN_TIMEOUT_ENV}")
    sys.exit(1)

if SCREEN_TIMEOUT <= 0:
    logging.info("SCREEN_TIMEOUT <= 0; exiting (nothing to manage)")
    sys.exit(0)

if not SWALLOW_FIRST_TOUCH:
    logging.info("SWALLOW_FIRST_TOUCH disabled; exiting")
    sys.exit(0)

class IdleSwallowDaemon:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.disp = display.Display()
        self.root = self.disp.screen().root
        # Select root events (fallback path if XScreenSaver extension not present)
        self.root.change_attributes(event_mask=(X.KeyPressMask | X.KeyReleaseMask |
                                                X.ButtonPressMask | X.ButtonReleaseMask |
                                                X.PointerMotionMask | X.StructureNotifyMask))
        self.overlay: Optional[int] = None
        self.blanked = False
        self.last_activity = time.time()  # Fallback only
        # Detect XScreenSaver extension availability
        try:
            # Query once to confirm availability
            _ = screensaver.query_info(self.disp, self.root).reply()
            self.ss_available = True
            logging.info("XScreenSaver extension detected for idle timing")
        except Exception:
            self.ss_available = False
            logging.info("XScreenSaver extension NOT available; falling back to event-based idle timing")
        # Attempt to enable XI2 raw events to improve fallback idle detection
        self.xi2_raw_enabled = False
        if not self.ss_available:
            try:
                xinput.query_version(self.disp, 2, 3)  # Ensure XI2 available
                # Build event mask for raw events
                masks = [xinput.RawMotion, xinput.RawKeyPress, xinput.RawKeyRelease,
                         xinput.RawButtonPress, xinput.RawButtonRelease]
                # Try to include touch if supported
                for maybe_touch in ("RawTouchBegin", "RawTouchEnd", "RawTouchUpdate"):
                    if hasattr(xinput, maybe_touch):
                        masks.append(getattr(xinput, maybe_touch))
                em = xinput.EventMask(deviceid=xinput.AllDevices, mask=masks)
                self.disp.xinput_select_events(self.root, [em])
                self.disp.flush()
                self.xi2_raw_enabled = True
                logging.info("XI2 raw event monitoring enabled for idle fallback")
            except Exception as e:
                logging.warning(f"Failed to enable XI2 raw events fallback: {e}")

    # -------------- Utility Commands --------------
    def _run(self, cmd: str):
        try:
            subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # pragma: no cover
            logging.warning(f"command failed '{cmd}': {e}")

    # -------------- Overlay Management --------------
    def create_overlay(self):
        if self.overlay is not None:
            return
        screen = self.disp.screen()
        width = screen.width_in_pixels
        height = screen.height_in_pixels
        # Create top-level override-redirect window
        win = self.root.create_window(
            0, 0, width, height, 0,
            screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=0x000000,  # black (invisible while off anyway)
            override_redirect=True,
            event_mask=(X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask | X.KeyPressMask | X.KeyReleaseMask)
        )
        # Raise & map
        win.map()
        self.disp.sync()
        self.overlay = win.id
        logging.info(f"Overlay created (id={self.overlay}) and mapped; screen blanked")

    def destroy_overlay(self):
        if self.overlay is None:
            return
        try:
            ov = self.disp.create_resource_object('window', self.overlay)
            ov.unmap()
            ov.destroy()
            self.disp.sync()
            logging.info(f"Overlay destroyed (id={self.overlay})")
        except Exception as e:  # pragma: no cover
            logging.warning(f"failed destroying overlay: {e}")
        finally:
            self.overlay = None

    # -------------- Blank / Wake Logic --------------
    def blank_screen(self):
        if self.blanked:
            return
        self.create_overlay()
        # Force DPMS off (panel off)
        self._run('xset dpms force off')
        self.blanked = True
        logging.info("Screen blanked (DPMS off)")

    def wake_screen(self):
        if not self.blanked:
            return
        self._run('xset dpms force on')
        time.sleep(0.05)
        self.destroy_overlay()
        self.blanked = False
        # Reset fallback activity baseline; XScreenSaver idle resets automatically due to input event
        self.last_activity = time.time()
        logging.info("Screen awakened; first touch swallowed")

    # -------------- Idle Time --------------
    def get_idle_seconds(self) -> float:
        if self.ss_available:
            try:
                info = screensaver.query_info(self.disp, self.root).reply()
                return info.idle / 1000.0  # ms -> s
            except Exception:
                # On error, disable extension for rest of session
                if self.ss_available:
                    logging.warning("XScreenSaver query failed; reverting to fallback")
                self.ss_available = False
        # Fallback: derive idle from last_activity maintained by input events we see
        return max(0.0, time.time() - self.last_activity)

    # -------------- Event Handling --------------
    def process_events(self, swallow_if_blanked=True):
        activity = False
        while self.disp.pending_events():
            ev = self.disp.next_event()
            etype = ev.type
            evtype_attr = getattr(ev, 'evtype', None)  # XI2 raw event type if GenericEvent

            # Helper: debug log builder (only if DEBUG enabled)
            def log_event(event_kind: str, action: str, extra: str = ""):
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    logging.debug(f"EVENT kind={event_kind} action={action} blanked={self.blanked} xi2_raw={self.xi2_raw_enabled and bool(evtype_attr)} {extra}")

            # Core event name mapping
            core_names = {
                X.KeyPress: 'KeyPress',
                X.KeyRelease: 'KeyRelease',
                X.ButtonPress: 'ButtonPress',
                X.ButtonRelease: 'ButtonRelease',
                X.MotionNotify: 'MotionNotify',
            }

            # ---- Core events ----
            if etype in core_names:
                if self.blanked and swallow_if_blanked:
                    log_event(core_names[etype], 'wake+swallow')
                    self.wake_screen()
                else:
                    if not self.ss_available:
                        self.last_activity = time.time()
                        log_event(core_names[etype], 'activity-update')
                    else:
                        log_event(core_names[etype], 'seen-no-update')
                activity = True
                continue

            # ---- XI2 raw events ----
            if self.xi2_raw_enabled and evtype_attr is not None:
                # Build XI2 raw name map lazily
                raw_map = {}
                for name in ("RawMotion","RawKeyPress","RawKeyRelease","RawButtonPress","RawButtonRelease","RawTouchBegin","RawTouchUpdate","RawTouchEnd"):
                    if hasattr(xinput, name):
                        raw_map[getattr(xinput, name)] = name
                raw_name = raw_map.get(evtype_attr, f"RawUnknown({evtype_attr})")
                if self.blanked and swallow_if_blanked and raw_name in ("RawButtonPress","RawTouchBegin","RawKeyPress"):
                    log_event(raw_name, 'wake+swallow')
                    self.wake_screen()
                else:
                    if not self.ss_available:
                        self.last_activity = time.time()
                        log_event(raw_name, 'activity-update')
                    else:
                        log_event(raw_name, 'seen-no-update')
                activity = True
        return activity

    # -------------- Main Loop --------------
    def run(self):
        logging.info(f"Started; timeout={self.timeout}s; swallowing first touch after blank")
        x_fd = self.disp.fileno()
        while True:
            if not self.blanked:
                idle_sec = self.get_idle_seconds()
                remaining = self.timeout - idle_sec
                if remaining <= 0:
                    self.blank_screen()
                    timeout = None  # Wait for input
                else:
                    timeout = remaining
            else:
                timeout = None
            rlist, _, _ = select.select([x_fd], [], [], timeout)
            if not rlist:
                # Timeout path triggers blank already above; continue
                continue
            self.process_events()


def main():
    try:
        daemon = IdleSwallowDaemon(SCREEN_TIMEOUT)
        daemon.run()
    except KeyboardInterrupt:
        logging.info("Exiting on SIGINT")
    except Exception as e:
        logging.critical(f"Unhandled exception: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
