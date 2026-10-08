"""
知序知识库 — 后端微服务
"""
__version__ = "3.1.0"
"""Application package with compatibility for the legacy script entry point."""

import sys
from pathlib import Path

_APP_DIRECTORY = str(Path(__file__).resolve().parent)
if _APP_DIRECTORY not in sys.path:
    sys.path.insert(0, _APP_DIRECTORY)
