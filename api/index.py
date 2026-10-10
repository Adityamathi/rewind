"""Vercel entrypoint: FastAPI app serving the Rewind demo (pre-recorded trace).

Demo mode: Vercel's filesystem is read-only/ephemeral, so `rewind run`
(recording new traces) is disabled here. The app serves a bundled
`demo_trace.db` (cart_negative_total, 24 steps) — asking questions,
scrubbing steps, and chain highlighting all work read-only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rewind.server import create_app

DEMO_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_trace.db")

app = create_app(DEMO_DB)
