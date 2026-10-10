"""SQLite trace store: schema + batched writer + read helpers."""
from __future__ import annotations

import os
import sqlite3
import sys
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS frames (
  frame_id     INTEGER PRIMARY KEY,
  parent_id    INTEGER,
  func         TEXT,
  call_step    INTEGER,
  return_step  INTEGER,
  return_value TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  step     INTEGER PRIMARY KEY,
  frame_id INTEGER,
  line     INTEGER,
  event    TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  id       INTEGER PRIMARY KEY,
  step     INTEGER,
  frame_id INTEGER,
  var      TEXT,
  old      TEXT,
  new      TEXT
);
CREATE TABLE IF NOT EXISTS outputs (step INTEGER, stream TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS exceptions (step INTEGER, frame_id INTEGER, type TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_changes_var  ON changes(var, step);
CREATE INDEX IF NOT EXISTS idx_changes_step ON changes(step);
CREATE INDEX IF NOT EXISTS idx_steps_frame  ON steps(frame_id, step);
"""


class TraceStore:
    """Batched writer used by the recorder. Keeps the trace callback fast."""

    def __init__(self, path: str, script_path: str = ""):
        first_time = not os.path.exists(path)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(SCHEMA)
        self._steps: list[tuple] = []
        self._changes: list[tuple] = []
        self._frames: list[tuple] = []
        self._outputs: list[tuple] = []
        self._exceptions: list[tuple] = []
        self._pending_returns: list[tuple] = []
        if first_time or script_path:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("script", script_path),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("python", sys.version.split()[0]),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                ("created", time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            self.conn.commit()

    # -- buffered adds -------------------------------------------------
    def add_frame(self, fid, parent, func, call_step):
        self._frames.append((fid, parent, func, call_step))

    def add_step(self, step, fid, line, event="line"):
        self._steps.append((step, fid, line, event))
        if len(self._steps) >= 2000:
            self.flush()

    def add_change(self, step, fid, var, old, new):
        if step is None or step < 0:
            return
        self._changes.append((step, fid, var, old, new))
        if len(self._changes) >= 2000:
            self.flush()

    def add_output(self, step, stream, text):
        self._outputs.append((step, stream, text))

    def add_exception(self, step, fid, exc_type, message):
        self._exceptions.append((step, fid, exc_type, message))

    def set_return(self, fid, step, value):
        self._pending_returns.append((step, value, fid))

    def flush(self):
        c = self.conn
        if self._frames:
            c.executemany(
                "INSERT OR REPLACE INTO frames(frame_id,parent_id,func,call_step) VALUES(?,?,?,?)",
                self._frames,
            )
            self._frames.clear()
        if self._steps:
            c.executemany(
                "INSERT OR REPLACE INTO steps(step,frame_id,line,event) VALUES(?,?,?,?)",
                self._steps,
            )
            self._steps.clear()
        if self._changes:
            c.executemany(
                "INSERT INTO changes(step,frame_id,var,old,new) VALUES(?,?,?,?,?)",
                self._changes,
            )
            self._changes.clear()
        if self._outputs:
            c.executemany(
                "INSERT INTO outputs(step,stream,text) VALUES(?,?,?)", self._outputs
            )
            self._outputs.clear()
        if self._exceptions:
            c.executemany(
                "INSERT INTO exceptions(step,frame_id,type,message) VALUES(?,?,?,?)",
                self._exceptions,
            )
            self._exceptions.clear()
        if self._pending_returns:
            c.executemany(
                "UPDATE frames SET return_step=?, return_value=? WHERE frame_id=?",
                self._pending_returns,
            )
            self._pending_returns.clear()
        c.commit()

    def close(self):
        try:
            self.flush()
        finally:
            self.conn.commit()
            self.conn.close()


def open_read(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
