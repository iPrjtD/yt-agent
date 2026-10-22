"""Subprocess helpers shared by the media code and the autopilot."""

import os
import subprocess

# Scheduled runs use pythonw (no console); without this flag every FFmpeg call would flash a window.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
