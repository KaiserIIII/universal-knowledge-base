"""
通用知识库 — 一键启动脚本 (纯 Python, 无编码问题)
用法: python start.py
"""
import os
import subprocess
import sys
import time
import webbrowser
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"
VENV_PYTHON = BACKEND_DIR / "venv" / "Scripts" / "python.exe"
ENV_FILE = BACKEND_DIR / ".env"
ENV_TEMPLATE = BACKEND_DIR / ".env.template"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"
FRONTEND_HTML = BASE_DIR / "index.html"
DATA_DIR = BACKEND_DIR / "app" / "data"


def step(msg: str):
    print(f"\n{'='*50}")
    print(f"  {msg}")
    print(f"{'='*50}")


def fail(msg: str):
    print(f"\n[ERROR] {msg}")
    sys.exit(1)


def wait_for_backend(url: str = "http://127.0.0.1:8000/health", timeout: int = 180) -> bool:
    """Wait until FastAPI and the local retrieval engine are ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    return False


def main():
    os.chdir(str(BASE_DIR))

    print("=" * 50)
    print("  Generic Knowledge Base v2.1 - Embedded Edition")
    print("  Zero external services required")
    print("=" * 50)

    # 1. Check Python
    step("Step 1/4: Checking Python...")
    result = subprocess.run([sys.executable, "--version"], capture_output=True, text=True)
    print(f"  {result.stdout.strip()}")

    # 2. Setup venv + install
    step("Step 2/4: Setting up virtual environment...")
    if not VENV_PYTHON.exists():
        print("  Creating venv...")
        subprocess.run([sys.executable, "-m", "venv", str(BACKEND_DIR / "venv")], check=True)

    print("  Installing packages (first run downloads BGE model ~400MB)...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip",
         "--break-system-packages", "-q"],
        cwd=str(BACKEND_DIR),
    )
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS),
         "--break-system-packages", "-q"],
        cwd=str(BACKEND_DIR),
    )
    if result.returncode != 0:
        print("  [WARN] Some packages may not have installed, trying to continue...")

    # 3. Check .env
    step("Step 3/4: Checking configuration...")
    if not ENV_FILE.exists():
        # Copy template
        with open(ENV_TEMPLATE, "r", encoding="utf-8") as src:
            with open(ENV_FILE, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        print("  Default configuration created. Configure it in the Web UI.")

    # 4. Launch
    step("Step 4/4: Starting Knowledge Base...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Start backend
    print("  Starting backend...")
    subprocess.Popen(
        [str(VENV_PYTHON), "run.py"],
        cwd=str(BACKEND_DIR),
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )

    print("  Waiting for backend (first run loads the local embedding model)...")
    if not wait_for_backend():
        print("  [WARN] Backend did not become ready within 180 seconds.")

    # Open the frontend served by FastAPI so UI and API share one origin.
    if FRONTEND_HTML.exists():
        frontend_url = "http://localhost:8000"
        print(f"  Opening frontend: {frontend_url}")
        webbrowser.open(frontend_url)
    else:
        html = BASE_DIR / "index.html"
        if html.exists():
            print(f"  Opening frontend: {html}")
            webbrowser.open(str(html))
        else:
            print(f"  [WARN] Frontend HTML not found at: {FRONTEND_HTML}")
            print(f"  Please open it manually.")

    print()
    print("=" * 50)
    print("  ALL SYSTEMS READY")
    print("=" * 50)
    print(f"  API Docs   : http://localhost:8000/docs")
    print(f"  Health     : http://localhost:8000/health")
    print(f"  Data       : {DATA_DIR}")
    print(f"  To stop    : close the backend console window")
    print("=" * 50)
    print()

    print("  Launcher finished. All settings are available in the Web UI.")

if __name__ == "__main__":
    main()
