"""Recorder + engine tests (no LLM needed)."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rewind.recorder import record
from rewind.engine import Engine

TMP = os.environ.get("TMP", "/tmp")


def _trace(demo: str) -> str:
    db = os.path.join(TMP, f"rewind_test_{os.path.basename(demo)}.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        except OSError:
            pass
    st = record(os.path.join(os.path.dirname(os.path.dirname(__file__)), demo), db)
    assert st["steps"] > 5, st
    return db


def _close(eng):
    try:
        eng.conn.close()
    except Exception:
        pass


def test_cart_chain():
    db = _trace(os.path.join("demos", "cart_negative_total.py"))
    eng = Engine(db)
    try:
        hist = eng.var_history("total")
        assert hist, "no history for total"
        assert any(str(h["new"]).startswith("-") for h in hist), hist
        tb = eng.trace_back("total", hist[-1]["step"])
        vars_seen = {n["var"] for n in tb["chain"]}
        assert "total" in vars_seen
        assert "discount" in vars_seen or "coupons" in vars_seen, tb
        assert tb["root_cause_step"] in {n["step"] for n in tb["chain"]}
    finally:
        _close(eng)
        try:
            os.remove(db)
        except OSError:
            pass


def test_pagination_records():
    db = _trace(os.path.join("demos", "off_by_one_pagination.py"))
    eng = Engine(db)
    try:
        assert eng.var_history("pages"), "no pages history"
        assert eng.var_history("missing"), "no missing history"
    finally:
        _close(eng)
        try:
            os.remove(db)
        except OSError:
            pass


def test_mutable_default_records():
    db = _trace(os.path.join("demos", "mutable_default_arg.py"))
    eng = Engine(db)
    try:
        outs = eng.get_output()
        assert any("Same object: True" in o["text"] for o in outs), outs
    finally:
        _close(eng)
        try:
            os.remove(db)
        except OSError:
            pass
