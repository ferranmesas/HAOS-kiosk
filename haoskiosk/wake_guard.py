#!/usr/bin/env python3
"""Swallow the tap that wakes a blanked screen so it does not also click whatever is under it.

When the X screensaver activates (screen blanks), grab the pointer so that the next press/release
(the tap that wakes the screen) goes to this process instead of the browser. The grab is dropped
when that tap is released, or shortly after the screen wakes without one (e.g., key press, REST
call). Event driven (MIT-SCREEN-SAVER notify): nothing runs while the screen is awake.
The server drops the grab automatically if this process exits.
"""
import select
import sys
import time

from Xlib import X, display
from Xlib.ext import screensaver

GRACE = 1.0  #Seconds after the screen wakes to wait for a tap before releasing the grab
BUTTONS = X.ButtonPressMask | X.ButtonReleaseMask

d = display.Display()
root = d.screen().root
ext = d.query_extension("MIT-SCREEN-SAVER")
if not ext.present:
    sys.exit("MIT-SCREEN-SAVER extension not available")
NOTIFY = ext.first_event  #Event code of ScreenSaverNotify
root.screensaver_select_input(screensaver.NotifyMask)

grabbed = False  #Holding the pointer grab
woke = None      #When the screen woke while grabbed
pressed = False  #Tap in progress


def grab():
    global grabbed, woke, pressed
    if root.grab_pointer(False, BUTTONS, X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE, X.CurrentTime):
        print("Could not grab pointer", flush=True)
        return
    grabbed, woke, pressed = True, None, False


def release():
    global grabbed
    d.ungrab_pointer(X.CurrentTime)
    d.flush()
    grabbed = False


if root.screensaver_query_info().state == screensaver.StateOn:  #Already blanked at startup
    grab()

while True:
    while d.pending_events():
        ev = d.next_event()
        if ev.type & 0x7f == NOTIFY:
            if ev.state == screensaver.StateOn and not grabbed:
                grab()
            elif ev.state == screensaver.StateOff and grabbed:
                woke = time.monotonic()
        elif ev.type == X.ButtonPress:
            pressed = True
        elif ev.type == X.ButtonRelease and grabbed:
            release()
    if grabbed and woke and not pressed and time.monotonic() - woke > GRACE:
        release()
    if not d.pending_events():
        select.select([d.fileno()], [], [], GRACE if grabbed and woke and not pressed else None)
