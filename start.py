"""Run the installed SaaS environment in the foreground; Ctrl+C stops it."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def main():
    executable = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    candidates = [ROOT / '.venv' / executable, ROOT / 'backend/venv' / executable]
    python = next((path for path in candidates if path.is_file()), Path(sys.executable))
    probe = subprocess.run([str(python), '-c', 'import fastapi,sqlalchemy,uvicorn,aiosqlite'],
                           capture_output=True, text=True)
    if probe.returncode:
        print('Install the locked environment first; see README quick start.', file=sys.stderr)
        return 1
    print('Enterprise Knowledge: http://localhost:8000 (Ctrl+C to stop)', flush=True)
    return subprocess.call([str(python), 'run.py'], cwd=ROOT / 'backend')

if __name__ == '__main__':
    raise SystemExit(main())
