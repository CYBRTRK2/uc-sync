#!/bin/bash
# UC API gatherer -> local Drive mirror; existing 3-hour LaunchAgent cadence.
exec /usr/bin/python3 -B "$HOME/.config/uc-sync/uc_sync_and_mirror.py"
