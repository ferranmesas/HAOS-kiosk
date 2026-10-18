#!/bin/bash
# Swallow the tap that wakes a blanked (DPMS) screen: when the monitor goes off,
# cover it with wake_overlay.py, which eats the next tap and exits.
# (xset can't subscribe to DPMS events, so poll.)
monitor_on() { xset q | grep -q "Monitor is On"; }

while true; do
    sleep 0.5
    monitor_on && continue
    python3 /wake_overlay.py &
    pid=$!
    on=0
    while kill -0 "$pid" 2>/dev/null; do  #Overlay exits itself on tap release
        sleep 0.5
        if monitor_on; then on=$((on + 1)); else on=0; fi
        (( on >= 3 )) && kill "$pid" 2>/dev/null  #Woken without a tap (e.g. REST call)
    done
done
