"""Query engine: deterministic tools over trace.db (the LLM may only touch the trace here)."""
from __future__ import annotations

import re
import sqlite3
from collections import deque

from .analysis import analyze_source
from .store import open_read

MAX_ROWS = 50


class Engine:
    def __init__(self, db_path: str, source_text: str = ""):
        self.db_path = db_path
        self.conn = open_read(db_path)
        self._source = source_text
        self._lineinfo = None
        if source_text:
            self._lineinfo = analyze_source(source_text)

    def _ensure_lineinfo(self):
        if self._lineinfo is None:
            try:
                src = self.source_text()
                self._lineinfo = analyze_source(src)
            except Exception:
                from .analysis import LineInfo
                self._lineinfo = LineInfo()
        return self._lineinfo

    # -- basic reads ---------------------------------------------------
    def meta(self) -> dict:
        out = {}
        for r in self.conn.execute("SELECT key,value FROM meta"):
            out[r["key"]] = r["value"]
        try:
            out["steps"] = self.conn.execute("SELECT COUNT(*) c FROM steps").fetchone()["c"]
        except Exception:
            out["steps"] = 0
        return out

    def source_text(self) -> str:
        if self._source:
            return self._source
        meta = self.meta()
        path = meta.get("script", "")
        try:
            with open(path, encoding="utf-8") as f:
                self._source = f.read()
        except Exception:
            self._source = ""
        return self._source

    def source_lines(self, start: int, end: int) -> list[dict]:
        src = self.source_text().splitlines()
        out = []
        for i in range(max(1, start), min(len(src), end) + 1):
            out.append({"line": i, "text": src[i - 1]})
        return out

    def var_history(self, name: str, frame_id: int | None = None, limit: int = MAX_ROWS) -> list[dict]:
        q = """SELECT c.step, s.line, c.frame_id, c.old, c.new FROM changes c
               LEFT JOIN steps s ON s.step=c.step WHERE c.var=?"""
        args: list = [name]
        if frame_id is not None:
            q += " AND c.frame_id=?"
            args.append(frame_id)
        q += " ORDER BY c.step LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def first_change_where(self, name: str, predicate: str) -> dict | None:
        """predicate like '< 0', '== None', 'in [...]' evaluated safely on parsed values."""
        hist = self.var_history(name, limit=1000)
        for h in hist:
            if _satisfies(h["new"], predicate):
                return h
        return None

    def stack_at(self, step: int) -> list[dict]:
        row = self.conn.execute("SELECT frame_id,line FROM steps WHERE step=?", (step,)).fetchone()
        if not row:
            # nearest earlier step
            row = self.conn.execute(
                "SELECT frame_id,line FROM steps WHERE step<=? ORDER BY step DESC LIMIT 1", (step,)
            ).fetchone()
            if not row:
                return []
        out, fid = [], row["frame_id"]
        seen = set()
        while fid and fid not in seen:
            seen.add(fid)
            fr = self.conn.execute("SELECT * FROM frames WHERE frame_id=?", (fid,)).fetchone()
            if not fr:
                break
            out.append({"frame_id": fid, "func": fr["func"], "line": row["line"] if not out else fr["call_step"]})
            fid = fr["parent_id"]
        return out

    def locals_at(self, step: int, frame_id: int | None = None) -> dict:
        if frame_id is None:
            fr = self.conn.execute(
                "SELECT frame_id FROM steps WHERE step<=? ORDER BY step DESC LIMIT 1", (step,)
            ).fetchone()
            frame_id = fr["frame_id"] if fr else 1
        rows = self.conn.execute(
            """SELECT var,new FROM changes WHERE frame_id=? AND step<=? ORDER BY step,id""",
            (frame_id, step),
        ).fetchall()
        state: dict = {}
        for r in rows:
            state[r["var"]] = r["new"]
        return {"frame_id": frame_id, "values": state}

    def diff_between(self, a: int, b: int) -> list[dict]:
        if a > b:
            a, b = b, a
        rows = self.conn.execute(
            """SELECT step,frame_id,var,old,new FROM changes
               WHERE step>? AND step<=? ORDER BY step LIMIT ?""",
            (a, b, MAX_ROWS),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_output(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute("SELECT step,stream,text FROM outputs ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_exception(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM exceptions ORDER BY step DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def step_info(self, step: int) -> dict:
        r = self.conn.execute("SELECT * FROM steps WHERE step=?", (step,)).fetchone()
        if not r:
            return {"step": step}
        d = dict(r)
        d.update(self.locals_at(step, d["frame_id"]))
        return d

    # -- trace_back: dynamic backward slice ----------------------------
    def trace_back(self, name: str, step: int, depth: int = 8) -> dict:
        info = self._ensure_lineinfo()
        chain: list[dict] = []
        visited: set[tuple] = set()
        queue: deque[tuple[str, int]] = deque([(name, step)])
        while queue and len(chain) < depth:
            var, st = queue.popleft()
            w = self.conn.execute(
                """SELECT c.step,s.line,c.frame_id,c.var,c.old,c.new FROM changes c
                   LEFT JOIN steps s ON s.step=c.step
                   WHERE c.var=? AND c.step<=? ORDER BY c.step DESC LIMIT 1""",
                (var, st),
            ).fetchone()
            if not w:
                continue
            key = (w["var"], w["step"])
            if key in visited:
                continue
            visited.add(key)
            line = w["line"] or 0
            node = {"step": w["step"], "line": line, "var": w["var"],
                    "old": w["old"], "new": w["new"], "frame_id": w["frame_id"]}
            chain.append(node)
            reads: set[str] = set(info.reads.get(line, set()))
            reads |= set(info.guards.get(line, set()))
            for r in sorted(reads):
                if r == var and len(reads) > 1:
                    queue.append((r, w["step"] - 1 if w["step"] else 0))
                elif r != var:
                    queue.append((r, w["step"] - 1 if w["step"] else 0))
            # self-accumulating lines (x = x + ...) need the earlier value too
            if var in info.reads.get(line, set()) and var in info.writes.get(line, set()):
                queue.append((var, w["step"] - 1 if w["step"] else 0))
        chain.sort(key=lambda n: n["step"])
        chain = _prune(chain, name)
        root = _pick_root(chain)
        return {"target": {"var": name, "step": step}, "chain": chain,
                "root_cause_step": root["step"] if root else (chain[0]["step"] if chain else step)}


def _meaningful(n: dict, target: str) -> bool:
    if n["var"] == target:
        return True
    o, v = n.get("old"), n.get("new")
    if o is None:
        return True
    os, vs = str(o), str(v)
    if _sign_flip(os, vs):
        return True
    # container length changed (append / dropped page / shared-list growth)
    if (os.startswith("[") and vs.startswith("[")) or (os.startswith("{") and vs.startswith("{")):
        return os != vs and abs(len(vs) - len(os)) > 4
    # first appearance of a value / big jump
    if len(vs) > len(os) + 20 or len(vs) < len(os) - 20:
        return True
    # skip <function ...> noise and trivial int bumps (loop counters)
    if vs.startswith("<function") or vs.startswith("<module"):
        return False
    try:
        if abs(float(vs) - float(os)) <= 1 and len(n["var"]) <= 2:
            return False
    except Exception:
        pass
    return True


def _prune(chain: list[dict], target: str) -> list[dict]:
    if len(chain) <= 3:
        return chain
    kept = [n for n in chain if _meaningful(n, target)]
    # always keep target's latest node
    if chain and all(n["var"] != target for n in kept):
        kept.append(chain[-1])
    kept.sort(key=lambda n: n["step"])
    return kept[:8] or chain[:8]


def _pick_root(chain: list[dict]) -> dict | None:
    if not chain:
        return None
    # Skip module/function definition noise; prefer earliest meaningful change.
    cands = [n for n in chain if not str(n.get("new", "")).startswith(("<function", "<module"))]
    pool = cands or chain
    for n in pool:
        o, v = str(n.get("old")), str(n.get("new"))
        if _sign_flip(o, v):
            return n
    for n in pool:
        if n.get("old") is None:
            return n
    return pool[0]


def _sign_flip(a: str, b: str) -> bool:
    try:
        return float(a) >= 0 > float(b) or float(a) <= 0 < float(b)
    except Exception:
        return False


def _satisfies(new_repr: str, predicate: str) -> bool:
    """Tiny safe predicate evaluator for first_change_where."""
    predicate = predicate.strip()
    m = re.match(r"^(==|!=|<=|>=|<|>)\s*(.+)$", predicate)
    if not m:
        # substring fallback
        return predicate.lower() in str(new_repr).lower()
    op, rhs = m.group(1), m.group(2).strip().strip("'\"")
    try:
        lhs = float(str(new_repr).strip().strip("'\""))
        rh = float(rhs)
        return {"==": lhs == rh, "!=": lhs != rh, "<": lhs < rh,
                ">": lhs > rh, "<=": lhs <= rh, ">=": lhs >= rh}[op]
    except Exception:
        l, r = str(new_repr), rhs
        if op == "==":
            return l == r
        if op == "!=":
            return l != r
        if op == "<":
            return l < r
        if op == ">":
            return l > r
        return False
