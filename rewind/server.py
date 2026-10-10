"""FastAPI server: `rewind serve trace.db` -> http://localhost:8000"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import ask
from .engine import Engine

STATIC_DIR = Path(__file__).parent / "static"


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="Rewind")
    db_abs = os.path.abspath(db_path)

    def eng() -> Engine:
        # fresh read-only connection per request: avoids SQLite thread-affinity issues
        e = Engine(db_abs)
        try:
            yield e
        finally:
            try:
                e.conn.close()
            except Exception:
                pass

    # Simpler: construct per request without generator complexity.
    def get_engine() -> Engine:
        return Engine(db_abs)

    class AskBody(BaseModel):
        question: str

    @app.get("/")
    def index():
        idx = STATIC_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"error": "UI not built"})

    @app.get("/api/meta")
    def meta():
        e = get_engine()
        try:
            m = e.meta()
            exc = e.get_exception()
            return {**m, "exception": exc}
        finally:
            e.conn.close()

    @app.get("/api/source")
    def source():
        e = get_engine()
        try:
            src = e.source_text().splitlines()
            return {"lines": [{"line": i + 1, "text": t} for i, t in enumerate(src)]}
        finally:
            e.conn.close()

    @app.get("/api/step/{n}")
    def step(n: int):
        e = get_engine()
        try:
            return e.step_info(n)
        finally:
            e.conn.close()

    @app.get("/api/range")
    def rng(frm: int = 1, to: int = 500):
        e = get_engine()
        try:
            to = min(to, frm + 2000)
            rows = e.conn.execute(
                "SELECT step,frame_id,line FROM steps WHERE step>=? AND step<=? ORDER BY step",
                (frm, to),
            ).fetchall()
            return {"steps": [dict(r) for r in rows]}
        finally:
            e.conn.close()

    @app.get("/api/history")
    def history(var: str):
        e = get_engine()
        try:
            return {"var": var, "history": e.var_history(var)}
        finally:
            e.conn.close()

    @app.post("/api/ask")
    def ask_post(body: dict):
        q = (body or {}).get("question", "")
        if not q:
            return {"error": "missing question", "answer": "Ask e.g. 'Why is the total negative?'", "chain": [], "root_cause_step": 1}
        return ask(q, db_abs)

    @app.websocket("/ws/ask")
    async def ws_ask(ws: WebSocket):
        await ws.accept()
        try:
            data = await ws.receive_json()
            q = data.get("question", "")
            await ws.send_json({"status": "checking output..."})
            out = ask(q, db_path)
            await ws.send_json({"status": "done", **out})
        except Exception as e:
            await ws.send_json({"error": str(e)})
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000):
    import uvicorn
    app = create_app(os.path.abspath(db_path))
    print(f"Rewind UI: http://{host}:{port}  (trace: {db_path})")
    uvicorn.run(app, host=host, port=port)
