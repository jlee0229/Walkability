"""Run every PWA checkpoint script in order against a local server.

    uvicorn pwa.server:app --port 7860 &
    python pwa/tests/run_all.py
"""
import pathlib
import subprocess
import sys

SCRIPTS = [
    "smoke_boot.py",
    "test_geometry.py",
    "test_nav_follow.py",
    "test_nav_reroute.py",
    "test_nav_geolocation.py",
]

here = pathlib.Path(__file__).parent
failed = []
for name in SCRIPTS:
    print(f"--- {name}")
    r = subprocess.run([sys.executable, str(here / name)], cwd=here)
    if r.returncode != 0:
        failed.append(name)
print("---")
if failed:
    print("FAILED:", ", ".join(failed))
    sys.exit(1)
print(f"All {len(SCRIPTS)} checkpoint scripts passed.")
