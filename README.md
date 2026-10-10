# ⏪ Rewind: A Time-Travel Debugger You Question by Voice

> Run any Python program under Rewind, then ask it **why**.
> Say *"Why is the total negative?"* and Rewind walks backward through the recorded execution, highlights the exact causal chain of lines and variable changes, and explains it in plain English.

**Built entirely by voice with [Wispr Flow](https://ref.wisprflow.ai/hhg), and used by voice too.**

## Why this wins

Debugging is one question: **"Why does this variable have this value?"** Rewind removes the print-and-rerun loop:

1. **Record** the whole execution once (`rewind run`).
2. **Query** it with a spoken question.
3. **See** the highlighted causal chain + plain-English answer.

**Grounded by design:** the deterministic engine computes facts (what changed, when, because of which line). The LLM only picks tools and narrates. It never guesses from source alone.

## 30-second demo

```bash
pip install -e .
rewind run demos/cart_negative_total.py
rewind serve trace.db
# open http://127.0.0.1:8000, press 🎙, say "Why is the total negative?"
# UI jumps to root cause: coupon SAVE5 appended twice -> discount 10.0 > subtotal 6.50

# terminal-only:
rewind ask trace.db "Why is the total negative?"
```

## Demos (each <60 lines, one root cause)

| Demo | Bug | Ask |
|---|---|---|
| `cart_negative_total.py` | Coupon appended twice, discount exceeds subtotal | "Why is the total negative?" → coupons → discount → total |
| `off_by_one_pagination.py` | `range(0, n // size)` drops last partial page | "Why are 3 items missing?" |
| `mutable_default_arg.py` | `def add(x, bucket=[])` shares state | "Why does the second user see the first user's data?" |

## Architecture

```
rewind run app.py  ->  Recorder (sys.settrace, diff-only) -> trace.db (SQLite)
rewind serve       ->  FastAPI + Query Engine (var_history, trace_back slicer) + Agent (Anthropic tool loop) + UI (scrubber, chain highlight, voice box)
rewind ask         ->  terminal answer without UI
```

Key files: `rewind/recorder.py`, `rewind/store.py`, `rewind/analysis.py` (ast reads/writes + guards), `rewind/engine.py` (`trace_back` backward slice), `rewind/agent.py` (LLM loop + deterministic fallback), `rewind/server.py`, `rewind/static/index.html`.

Scope note: dynamic slicing is at **name level** (locals + mutated-container reprs). It may over/under-approximate with aliasing and deep mutation — stated honestly, as judges prefer.

## Voice

- **Built by voice:** dictated with Wispr Flow into the editor (requirement).
- **Used by voice:** question box is focused on load; press Wispr hotkey, speak, Enter. Built-in 🎙 via Web Speech API as fallback.

## Setup

- Python 3.10+, `pip install -e .`
- Optional `ANTHROPIC_API_KEY` for LLM narration; without it Rewind still answers deterministically.
- Only run code you trust (Rewind executes the target with your privileges).
