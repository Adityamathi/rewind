"""Recorder: run a target Python program under sys.settrace and persist a trace."""
from __future__ import annotations

import os
import runpy
import sys
import traceback

from .store import TraceStore

_MISSING = object()
MAX_REPR = 200
DEFAULT_STEP_LIMIT = 200_000


def safe_repr(v) -> str:
    try:
        r = repr(v)
    except Exception:
        return "<unrepresentable>"
    if len(r) > MAX_REPR:
        return r[:MAX_REPR] + "…"
    return r


class _StdoutTap:
    """Tags each write with the current step so the UI can place prints on the timeline."""

    def __init__(self, store: TraceStore, get_step, stream="stdout"):
        self._store = store
        self._get_step = get_step
        self._stream = stream
        self._orig = sys.stdout if stream == "stdout" else sys.stderr

    def write(self, s):
        if s:
            try:
                self._store.add_output(self._get_step(), self._stream, s)
            except Exception:
                pass
        return self._orig.write(s)

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def flush(self):
        return self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


class Recorder:
    def __init__(self, target_path: str, store: TraceStore, step_limit: int = DEFAULT_STEP_LIMIT):
        self.target = os.path.abspath(target_path)
        self.target_dir = os.path.dirname(self.target)
        self.store = store
        self.step = 0
        self.step_limit = step_limit
        self.frame_ids: dict[int, int] = {}
        self.prev_locals: dict[int, dict] = {}
        self.next_frame_id = 1
        self.stopped = False

    # -- snapshots -----------------------------------------------------
    def _snap(self, frame) -> dict:
        out = {}
        for k, v in frame.f_locals.items():
            if k.startswith("__"):
                continue
            out[k] = safe_repr(v)
        return out

    def _flush_changes(self, frame, fid: int):
        new = self._snap(frame)
        old = self.prev_locals.get(fid, {})
        # Attribute to the line that just ran (step - 1); line events fire *before* execution.
        cause_step = self.step - 1 if self.step >= 1 else 0
        if self.step == 0:
            self.prev_locals[fid] = new
            return
        for k, v in new.items():
            o = old.get(k, _MISSING)
            if o is _MISSING or o != v:
                self.store.add_change(
                    cause_step, fid, k,
                    None if o is _MISSING else o, v,
                )
        self.prev_locals[fid] = new

    # -- trace callback ------------------------------------------------
    def trace(self, frame, event, arg):
        if self.stopped:
            return None
        try:
            fname = os.path.abspath(frame.f_code.co_filename)
        except Exception:
            return self.trace
        # Only trace the target file (stdlib / third-party ignored => 10x+ faster).
        if fname != self.target:
            return None
        fid = self.frame_ids.get(id(frame))
        if event == "call":
            fid = self.next_frame_id
            self.next_frame_id += 1
            self.frame_ids[id(frame)] = fid
            parent = self.frame_ids.get(id(frame.f_back)) if frame.f_back else None
            self.store.add_frame(fid, parent, frame.f_code.co_name, self.step)
            self.prev_locals[fid] = {}
            return self.trace
        if fid is None:
            return self.trace
        if event in ("line", "return", "exception"):
            try:
                self._flush_changes(frame, fid)
            except Exception:
                pass
        if event == "line":
            self.step += 1
            if self.step > self.step_limit:
                self.stopped = True
                self.store.add_output(self.step, "stderr", f"\n[rewind] step limit {self.step_limit} hit — trace truncated.\n")
                sys.settrace(None)
                return None
            self.store.add_step(self.step, fid, frame.f_lineno, "line")
        elif event == "return":
            self.store.set_return(fid, self.step, safe_repr(arg))
        elif event == "exception":
            try:
                exc_type, exc_val, _tb = arg
                self.store.add_exception(
                    self.step, fid,
                    getattr(exc_type, "__name__", str(exc_type)),
                    str(exc_val)[:500],
                )
            except Exception:
                pass
        return self.trace

    def run(self) -> dict:
        tap_out = _StdoutTap(self.store, lambda: self.step, "stdout")
        tap_err = _StdoutTap(self.store, lambda: self.step, "stderr")
        old_out, old_err = sys.stdout, sys.stderr
        old_argv = sys.argv[:]
        old_path0 = sys.path[0] if sys.path else None
        sys.stdout, sys.stderr = tap_out, tap_err
        sys.argv = [self.target]
        if self.target_dir not in sys.path:
            sys.path.insert(0, self.target_dir)
        sys.settrace(self.trace)
        status = {"steps": 0, "error": None}
        try:
            runpy.run_path(self.target, run_name="__main__")
        except SystemExit as e:
            status["exit"] = getattr(e, "code", 0)
        except BaseException as e:  # record uncaught exception, then keep trace
            try:
                self.store.add_exception(self.step, 0, type(e).__name__, str(e)[:500])
            except Exception:
                pass
            status["error"] = f"{type(e).__name__}: {e}"
            status["traceback"] = traceback.format_exc(limit=5)
        finally:
            sys.settrace(None)
            sys.stdout, sys.stderr = old_out, old_err
            sys.argv = old_argv
            try:
                if old_path0 is None:
                    pass
            except Exception:
                pass
            try:
                self.store.flush()
            except Exception:
                pass
            status["steps"] = self.step
        return status


def record(target: str, db_path: str, step_limit: int = DEFAULT_STEP_LIMIT) -> dict:
    store = TraceStore(db_path, script_path=os.path.abspath(target))
    rec = Recorder(target, store, step_limit=step_limit)
    try:
        return rec.run()
    finally:
        try:
            store.close()
        except Exception:
            pass
