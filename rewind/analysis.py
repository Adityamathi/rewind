"""Static analysis: per-line read/write sets + enclosing-condition deps via ast."""
from __future__ import annotations

import ast
from collections import defaultdict


class LineInfo:
    def __init__(self):
        self.reads: dict[int, set[str]] = defaultdict(set)
        self.writes: dict[int, set[str]] = defaultdict(set)
        # line -> set of vars read by enclosing if/while/for conditions
        self.guards: dict[int, set[str]] = defaultdict(set)


def _names(node: ast.AST) -> tuple[set[str], set[str]]:
    """Return (reads, writes) for Name nodes under `node`."""
    reads, writes = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Store):
                writes.add(n.id)
            elif isinstance(n.ctx, (ast.Load, ast.Del)):
                reads.add(n.id)
    return reads, writes


def analyze_source(source: str) -> LineInfo:
    info = LineInfo()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return info

    # Per-line reads/writes: attribute each Name to its own lineno.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ln = getattr(node, "lineno", None)
            if ln is None:
                continue
            if isinstance(node.ctx, ast.Store):
                info.writes[ln].add(node.id)
            else:
                info.reads[ln].add(node.id)

    # Attribute function args / for-targets / with-items / except handlers as writes
    # on their definition line (Name nodes there are often Load/Param).
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ln = node.lineno
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                info.writes[ln].add(a.arg)
            if node.args.vararg:
                info.writes[ln].add(node.args.vararg.arg)
            if node.args.kwarg:
                info.writes[ln].add(node.args.kwarg.arg)
        elif isinstance(node, ast.For):
            r, w = _names(node.target)
            info.writes[node.lineno] |= w | r
            # loop iterable reads belong to the for line too
            r2, _ = _names(node.iter)
            info.reads[node.lineno] |= r2

    # Control dependence: lines inside an if/while body inherit condition reads.
    def visit(node: ast.AST, guard_stack: list[set[str]]):
        cond_reads: set[str] = set()
        if isinstance(node, (ast.If, ast.While)):
            cond_reads, _ = _names(node.test)
        elif isinstance(node, ast.For):
            cond_reads, _ = _names(node.iter)
        new_stack = guard_stack + [cond_reads] if cond_reads else guard_stack
        ln = getattr(node, "lineno", None)
        if ln is not None and isinstance(node, ast.stmt) and guard_stack:
            merged: set[str] = set()
            for g in guard_stack:
                merged |= g
            if merged:
                info.guards[ln] |= merged
        for child in ast.iter_child_nodes(node):
            visit(child, new_stack)

    visit(tree, [])
    return info


def enclosing_condition_reads(info: LineInfo, line: int) -> set[str]:
    return set(info.guards.get(line, set()))
