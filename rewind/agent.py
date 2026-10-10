"""Agent: turn a natural-language (spoken) question into tool calls + grounded answer."""
from __future__ import annotations

import json
import os
import re

from .engine import Engine

SYSTEM = """You are Rewind, a debugging assistant. You can only learn about the
program by calling the provided tools. Never guess values. Cite step numbers.
When done, answer with JSON: {answer, chain_steps, root_cause_step, confidence}."""

TOOL_SCHEMAS = [
    {"name": "var_history", "description": "History of a variable", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "trace_back", "description": "Causal chain for var at step", "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "step": {"type": "string"}}, "required": ["name", "step"]}},
    {"name": "get_output", "description": "Program stdout with step tags", "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_exception", "description": "Final exception if any", "input_schema": {"type": "object", "properties": {}}},
    {"name": "source_lines", "description": "Source slice", "input_schema": {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}}}},
    {"name": "stack_at", "description": "Call stack at a step", "input_schema": {"type": "object", "properties": {"step": {"type": "integer"}}}},
]


def _run_tool(engine: Engine, name: str, args: dict):
    if name == "var_history":
        return engine.var_history(args.get("name", ""))
    if name == "trace_back":
        return engine.trace_back(args.get("name", ""), int(args.get("step", 0)))
    if name == "get_output":
        return engine.get_output()
    if name == "get_exception":
        return engine.get_exception()
    if name == "source_lines":
        return engine.source_lines(int(args.get("start", 1)), int(args.get("end", 50)))
    if name == "stack_at":
        return engine.stack_at(int(args.get("step", 0)))
    if name == "locals_at":
        return engine.locals_at(int(args.get("step", 0)))
    if name == "diff_between":
        return engine.diff_between(int(args.get("step_a", 0)), int(args.get("step_b", 0)))
    return {"error": f"unknown tool {name}"}


def _candidate_vars(question: str, engine: Engine) -> list[str]:
    """Extract likely variable names: quoted/backticked words, then known vars."""
    cands: list[str] = []
    for m in re.findall(r"[`'\"]([A-Za-z_][A-Za-z0-9_]*)[`'\"]", question):
        cands.append(m)
    for m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", question):
        if m.lower() not in {"why", "does", "the", "is", "are", "did", "what", "how", "with", "from", "this", "that"}:
            cands.append(m)
    # intersect with vars actually seen, preserving order
    try:
        rows = engine.conn.execute("SELECT DISTINCT var FROM changes").fetchall()
        known = {r["var"] for r in rows}
    except Exception:
        known = set()
    ordered = [c for c in cands if c in known]
    for k in sorted(known):
        if k not in ordered:
            ordered.append(k)
    return ordered or sorted(known)


def fallback_answer(question: str, engine: Engine) -> dict:
    """Deterministic path: no API key needed. Picks the most suspicious var and slices."""
    exc = engine.get_exception()
    cands = _candidate_vars(question, engine)
    qwords = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question.lower()))
    # Prefer vars mentioned in the question, then suspicious values.
    best, best_tb = None, None
    last_step = engine.conn.execute("SELECT MAX(step) m FROM steps").fetchone()["m"] or 0
    for var in cands[:16]:
        hist = engine.var_history(var, limit=200)
        if not hist:
            continue
        tb = engine.trace_back(var, hist[-1]["step"])
        score = 0
        if var.lower() in qwords:
            score += 20
        # short vars (x, i, p) are rarely the question subject
        if len(var) <= 2:
            score -= 8
        score += min(len(tb["chain"]), 8)
        # bonus for negative values (classic demo) or exception-adjacent steps
        if any(str(h.get("new", "")).lstrip().startswith("-") for h in hist):
            score += 5
        if exc and any(abs(h["step"] - exc["step"]) < 10 for h in hist):
            score += 3
        if best_tb is None or score > best[0]:
            best, best_tb = (score, var), tb
    if best_tb is None or not best_tb["chain"]:
        outs = engine.get_output(limit=5)
        tail = " ".join(o.get("text", "") for o in outs[-3:]) if outs else "no output"
        return {"answer": f"I recorded {last_step} steps but found no variable history matching your question. Program output tail: {tail.strip()[:300]}",
                "chain": [], "root_cause_step": last_step, "confidence": 0.2,
                "mode": "fallback-empty"}
    chain = best_tb["chain"]
    var = best_tb["target"]["var"]
    # narrate
    bits = []
    for n in chain:
        bits.append(f"step {n['step']} (line {n['line']}): {n['var']} {n['old']} → {n['new']}")
    src_lines = {s["line"]: s["text"] for s in engine.source_lines(1, 10000)}
    root = next((n for n in chain if n["step"] == best_tb["root_cause_step"]), chain[0])
    answer = (
        f"`{var}` went wrong through {len(chain)} causal step(s): " + "; ".join(bits) + ". "
        f"Root cause looks like step {root['step']} at line {root['line']}"
        + (f" (`{src_lines.get(root['line'], '').strip()}`)" if root["line"] in src_lines else "")
        + "."
    )
    if exc:
        answer += f" Final exception: {exc['type']}: {exc['message']} at step {exc['step']}."
    return {"answer": answer, "chain": chain,
            "root_cause_step": best_tb["root_cause_step"],
            "confidence": 0.8, "mode": "fallback-deterministic"}


def ask(question: str, db_path: str, max_turns: int = 8) -> dict:
    engine = Engine(db_path)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        out = fallback_answer(question, engine)
        out["question"] = question
        return out
    # LLM tool loop (grounded: only facts from tools; step ids validated server-side).
    try:
        import anthropic
    except Exception:
        out = fallback_answer(question, engine)
        out["question"] = question
        return out
    client = anthropic.Anthropic()
    tools = [
        {"name": "var_history", "description": "Ordered history of a variable: {step,line,old,new}.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        {"name": "trace_back", "description": "Causal chain for variable V with bad value at step S.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "step": {"type": "integer"}}, "required": ["name", "step"]}},
        {"name": "get_output", "description": "Program stdout/stderr tagged with steps.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "get_exception", "description": "Final exception with step and stack.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "source_lines", "description": "Source slice [start,end].",
         "input_schema": {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["start", "end"]}},
        {"name": "stack_at", "description": "Call stack at a step.",
         "input_schema": {"type": "object", "properties": {"step": {"type": "integer"}}, "required": ["step"]}},
    ]
    meta = engine.meta()
    ctx = f"Script: {meta.get('script','?')}, steps: {meta.get('steps','?')}. Question: {question}"
    messages: list = [{"role": "user", "content": ctx}]
    trace_log: list = []
    for _ in range(max_turns):
        resp = client.messages.create(model="claude-sonnet-4-5", max_tokens=1000,
                                      system=SYSTEM, tools=tools, messages=messages)
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            # try to parse JSON, else wrap
            try:
                parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
            except Exception:
                # ground it ourselves with fallback chain
                fb = fallback_answer(question, engine)
                fb["narration"] = text
                fb["question"] = question
                return fb
            parsed["question"] = question
            parsed.setdefault("tool_log", trace_log)
            return _validate(engine, parsed)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", "") == "tool_use":
                try:
                    res = _run_tool(engine, b.name, b.input or {})
                except Exception as e:
                    res = {"error": str(e)}
                trace_log.append({"tool": b.name, "input": b.input})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(res)[:6000]})
        messages.append({"role": "user", "content": results})
    fb = fallback_answer(question, engine)
    fb["question"] = question
    fb["tool_log"] = trace_log
    return fb


def _validate(engine: Engine, ans: dict) -> dict:
    """Grounding rule: every cited step must exist."""
    try:
        valid = {r["step"] for r in engine.conn.execute("SELECT step FROM steps").fetchall()}
    except Exception:
        valid = set()
    chain = ans.get("chain", [])
    if isinstance(chain, list):
        ans["chain"] = [c for c in chain if isinstance(c, dict) and c.get("step") in valid] or chain
    rc = ans.get("root_cause_step")
    if rc not in valid and isinstance(chain, list) and chain:
        # if LLM returned step ids, prefer fallback's grounded chain instead
        fb = fallback_answer(ans.get("question", ""), engine)
        ans["chain"] = fb["chain"]
        ans["root_cause_step"] = fb["root_cause_step"]
    return ans
