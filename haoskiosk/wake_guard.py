#!/usr/bin/env python3
# Swallow the tap that wakes a blanked screen so it doesn't click the dashboard underneath.
# Event-driven (MIT-SCREEN-SAVER notify, no polling): when the screen blanks, start
# wake_overlay.py, which eats the next tap and exits. If the screen wakes without a tap
# (key press, REST call), stop the overlay after GRACE seconds.
import select
import subprocess
import sys
import time

from Xlib import display
from Xlib.ext import screensaver

GRACE = 1.0  #Seconds after the screen wakes to wait for the overlay to see a tap

d = display.Display()
if not d.has_extension("MIT-SCREEN-SAVER"):
    sys.exit("MIT-SCREEN-SAVER extension not available")
d.screen().root.screensaver_select_input(screensaver.NotifyMask)
overlay = None  #Running wake_overlay.py process
woke = None     #When the screen woke while overlay was up

while True:
    if overlay and overlay.poll() is not None:  #Overlay exited on tap
        overlay = woke = None
    while d.pending_events():
        state = getattr(d.next_event(), "state", None)
        if state == screensaver.StateOn and not overlay:
            print("Screen blanked: starting overlay", flush=True)
            overlay = subprocess.Popen([sys.executable, "/wake_overlay.py"])
            woke = None
        elif state == screensaver.StateOff and overlay:
            woke = time.monotonic()
    if overlay and woke and time.monotonic() - woke > GRACE:  #Woken without a tap
        print("Screen woke without a tap: stopping overlay", flush=True)
        overlay.terminate()
        overlay.wait()
        overlay = woke = None
    if not d.pending_events():
        select.select([d.fileno()], [], [], GRACE if overlay and woke else None)
