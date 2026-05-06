"""
Vercel Python serverless entry. The real Flask app is defined in app.py at repo root.
This file exposes `app` for the runtime WSGI handler.
"""
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from app import app  # noqa: E402,F401 — Vercel expects name `app`
