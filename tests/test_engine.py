"""Engine + trace_back tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rewind.recorder import record
from rewind.engine import Engine
from rewind.agent import fallback_answer

TMP = os.environ.get("TMP", "/tmp")


def _db_for(demo):
    db = os.path.join(TMP, "rewind_eng_test.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        except OSError:
            pass
    record(os.path.join(os.path.dirname(os.path.dirname(__file__)), demo), db)
    return db


def _close(eng):
    try:
        eng.conn.close()
    except Exception:
        pass


def test_trace_back_cart_root():
    db = _db_for(os.path.join("demos", "cart_negative_total.py"))
    eng = Engine(db)
    try:
        ans = fallback_answer("Why is the total negative?", eng)
        assert ans["chain"], ans
        assert ans["root_cause_step"], ans
        # hero demo: root cause must be the double-append line (coupons)
        root = next(n for n in ans["chain"] if n["step"] == ans["root_cause_step"])
        assert root["var"] == "coupons", ans
    finally:
        _close(eng)
        try:
            os.remove(db)
        except OSError:
            pass


def test_engine_tools():
    db = _db_for(os.path.join("demos", "cart_negative_total.py"))
    eng = Engine(db)
    try:
        assert eng.meta()["steps"] > 0
        assert eng.source_lines(1, 3)
        assert eng.locals_at(5)
        assert eng.diff_between(1, 10) is not None
        assert eng.get_output()
    finally:
        _close(eng)
        try:
            os.remove(db)
        except OSError:
            pass
