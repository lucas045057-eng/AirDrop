"""Runs only inside the secret-free Linux Runner; -I ignores host Python settings."""
import os
import runpy
import sys
from pathlib import Path

root=Path(sys.argv[1]).resolve()
entry=(root/sys.argv[2]).resolve()
if not entry.is_relative_to(root):raise SystemExit('unsafe entrypoint')
os.chdir(root)
sys.path.insert(0,str(root))
sys.path.insert(0,str(entry.parent))
sys.argv=[str(entry),*sys.argv[3:]]
runpy.run_path(str(entry),run_name='__main__')
