#!/usr/bin/python3
# Backwards-compatible manual Drive entrypoint.
import subprocess,sys
from pathlib import Path
sys.exit(subprocess.call(['/usr/bin/python3','-B',str(Path(__file__).with_name('uc_mirror.py')),'drive']))
