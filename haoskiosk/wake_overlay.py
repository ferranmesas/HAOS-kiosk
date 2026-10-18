#!/usr/bin/env python3
# Fullscreen black window that grabs the pointer and exits when the touch/click
# that woke the screen is released, so that tap never reaches the browser.
from Xlib import X, display

d = display.Display()
s = d.screen()
g = s.root.get_geometry()
w = s.root.create_window(0, 0, g.width, g.height, 0, s.root_depth, X.InputOutput, X.CopyFromParent,
                         background_pixel=s.black_pixel, override_redirect=True,
                         event_mask=X.ButtonPressMask | X.ButtonReleaseMask)
w.map()
w.grab_pointer(False, X.ButtonPressMask | X.ButtonReleaseMask, X.GrabModeAsync, X.GrabModeAsync, w, X.NONE, X.CurrentTime)
while d.next_event().type != X.ButtonRelease:
    pass
