# Rewind: A Time-Travel Debugger You Question by Voice

> Run any Python program under Rewind, then ask it **why**.
> Say *"Why is the total negative?"* and Rewind walks backward through the recorded execution, highlights the exact causal chain of lines and variable changes, and explains it in plain English.

---

## 1. Problem and Idea

Debugging is mostly one question: **"Why does this variable have this value?"**

Today you answer it by adding `print` statements, re-running, stepping through a debugger, and holding the history in your head. Rewind removes that loop:

1. **Record** the whole execution once.
2. **Query** the recording with a natural-language (spoken) question.
3. **See** the answer as a highlighted causal chain in a UI, plus a plain-English explanation.

**Key design principle:** the *engine* computes facts (what changed, when, because of which line). The *LLM* only chooses which question to ask the engine and narrates the result. The LLM never guesses from source code alone, so answers are grounded in what actually happened.

---

## 2. High-Level Architecture

```
                         ┌──────────────────────────────────────────┐
                         │                Browser UI                │
                         │  Code view | Scrubber | Variables panel  │
                         │  Chain highlight | Voice/Text question   │
                         └───────────────▲──────────────┬───────────┘
                                         │ JSON/WebSocket│ question
                                         │               ▼
┌───────────────┐   trace.db   ┌─────────┴──────────────────────────┐
│  Recorder     │─────────────▶│            Rewind Server           │
│ (sys.settrace)│              │  ┌──────────┐   ┌───────────────┐  │
└───────▲───────┘              │  │ REST API │──▶│ Agent (LLM    │  │
        │                      │  └──────────┘   │ tool loop)    │  │
  rewind run app.py            │        │        └──────┬────────┘  │
        │                      │        ▼               │ tool calls│
 ┌──────┴───────┐              │  ┌──────────────────────▼───────┐  │
 │ Target Python│              │  │        Query Engine          │  │
 │   program    │              │  │ var_history, stack_at, diff, │  │
 └──────────────┘              │  │ trace_back (causal slicer)   │  │
                               │  └──────────────┬───────────────┘  │
                               │                 ▼                  │
                               │          SQLite trace store        │
                               └────────────────────────────────────┘
```

### Components at a glance

| Component | Responsibility | Tech |
|---|---|---|
| **Recorder** | Run the target program and capture every step | Python `sys.settrace` (or `sys.monitoring` on 3.12+) |
| **Trace Store** | Persist steps, frames, variable changes, output | SQLite |
| **Query Engine** | Deterministic functions over the trace | Pure Python + `ast` |
| **Agent** | Turn a question into tool calls and a narrated answer | Anthropic API with tool use |
| **Server** | Serve the UI and expose the API | FastAPI |
| **UI** | Visualize, scrub, ask, highlight | Single-page HTML/JS (no build step) |

---

## 3. End-to-End Pipeline

```
Phase 1: RECORD      rewind run buggy.py
                     └─▶ trace events ─▶ buffered writer ─▶ trace.db

Phase 2: INDEX       build indexes on (var, step), (frame), (line)
                     parse source with ast ─▶ per-line read/write sets

Phase 3: SERVE       rewind serve trace.db  ─▶ http://localhost:8000

Phase 4: ASK         user speaks/types question
                     └─▶ Agent (LLM) decides which tools to call

Phase 5: ANALYZE     Query Engine runs tools over trace.db
                     └─▶ facts + candidate causal chain (step ids)

Phase 6: EXPLAIN     LLM writes a short explanation citing step ids
                     └─▶ { answer, chain: [step ids], root_cause_step }

Phase 7: VISUALIZE   UI jumps to root cause, highlights chain,
                     shows variable values at each chain step
```

---

## 4. The Recorder

### 4.1 How it works

`sys.settrace` installs a callback that Python calls on every:

| Event | Fires when | We record |
|---|---|---|
| `call` | a function is entered | new frame, function name, arguments |
| `line` | a new line is about to execute | step, file, line number |
| `return` | a function returns | return value |
| `exception` | an exception is raised | exception type and message |

### 4.2 Capturing variable changes (diff-only)

Storing every local at every step is too large. Instead, keep the previous snapshot **per frame** and store only differences.

```python
# recorder.py (simplified)
import sys, os

class Recorder:
    def __init__(self, target_path, store):
        self.target = os.path.abspath(target_path)
        self.store = store
        self.step = 0
        self.frame_ids = {}        # frame object id -> frame_id
        self.prev_locals = {}      # frame_id -> dict of repr snapshots
        self.next_frame_id = 1

    def _snap(self, frame):
        out = {}
        for k, v in frame.f_locals.items():
            if k.startswith("__"):
                continue
            out[k] = safe_repr(v)  # truncated, never raises
        return out

    def _flush_changes(self, frame, fid):
        # Variables can only change between line events, so we diff
        # at the NEXT event of the same frame (line/return/exception).
        new = self._snap(frame)
        old = self.prev_locals.get(fid, {})
        for k, v in new.items():
            if old.get(k, _MISSING) != v:
                self.store.add_change(self.step - 1, fid, k, old.get(k), v)
        self.prev_locals[fid] = new

    def trace(self, frame, event, arg):
        if os.path.abspath(frame.f_code.co_filename) != self.target:
            return None            # ignore stdlib / third-party code
        fid = self.frame_ids.get(id(frame))
        if event == "call":
            fid = self.next_frame_id; self.next_frame_id += 1
            self.frame_ids[id(frame)] = fid
            parent = self.frame_ids.get(id(frame.f_back))
            self.store.add_frame(fid, parent, frame.f_code.co_name, self.step)
            self.prev_locals[fid] = {}
        elif event in ("line", "return", "exception"):
            self._flush_changes(frame, fid)
        if event == "line":
            self.step += 1
            self.store.add_step(self.step, fid, frame.f_lineno, "line")
        elif event == "return":
            self.store.set_return(fid, self.step, safe_repr(arg))
        elif event == "exception":
            self.store.add_exception(self.step, fid, arg)
        return self.trace

    def run(self):
        sys.settrace(self.trace)
        try:
            runpy.run_path(self.target, run_name="__main__")
        finally:
            sys.settrace(None)
            self.store.close()
```

### 4.3 Important details

- **Off-by-one in change attribution.** A `line` event fires *before* the line runs. So a change caused by line N is only visible at the next event. The recorder therefore attributes changes to `step - 1` (the line that just ran). Test this first; most bugs in the whole project will live here.
- **Filter to user code only.** Trace only files inside the target project. This cuts overhead by 10x or more.
- **Safe repr.** Wrap `repr()` in try/except and truncate to about 200 characters. Objects with broken `__repr__` must not crash the recorder.
- **Mutable objects.** `cart.items.append(x)` changes an object without rebinding a name, so a shallow name-comparison misses it. Compare the *repr string* of the value (cheap and catches mutation for lists, dicts, and simple objects).
- **Captured stdout.** Redirect `sys.stdout` through a wrapper that tags each write with the current step, so the UI can show "this print happened at step 214".
- **Step limit.** Default cap of about 200,000 steps with a clear warning, so a runaway loop cannot fill the disk.

---

## 5. Trace Store (SQLite)

### 5.1 Schema

```sql
CREATE TABLE meta   (key TEXT PRIMARY KEY, value TEXT);      -- script path, python version, start time

CREATE TABLE frames (
  frame_id     INTEGER PRIMARY KEY,
  parent_id    INTEGER,           -- caller frame (NULL for module level)
  func         TEXT,
  call_step    INTEGER,
  return_step  INTEGER,
  return_value TEXT
);

CREATE TABLE steps (
  step     INTEGER PRIMARY KEY,
  frame_id INTEGER,
  line     INTEGER,
  event    TEXT                   -- line | exception
);

CREATE TABLE changes (            -- the heart of the system
  id       INTEGER PRIMARY KEY,
  step     INTEGER,               -- step whose execution caused it
  frame_id INTEGER,
  var      TEXT,
  old      TEXT,
  new      TEXT
);

CREATE TABLE outputs (step INTEGER, stream TEXT, text TEXT);
CREATE TABLE exceptions (step INTEGER, frame_id INTEGER, type TEXT, message TEXT);

CREATE INDEX idx_changes_var  ON changes(var, step);
CREATE INDEX idx_changes_step ON changes(step);
CREATE INDEX idx_steps_frame  ON steps(frame_id, step);
```

### 5.2 Why SQLite

- Single file, zero setup, easy to ship as a demo artifact.
- Indexed range queries ("changes to `total` between step 100 and 500") are exactly what we need.
- Writes are batched (a few thousand rows per transaction) so recording stays fast.

### 5.3 Writer design

The trace callback must stay fast. It pushes tuples into an in-memory list, and a flush happens every N rows (or at exit) with `executemany`. No disk I/O inside the callback.

---

## 6. Query Engine

All functions are deterministic and return small JSON-friendly results. The LLM can only reach the trace through these.

### 6.1 Tool catalog

| Tool | Signature | Returns |
|---|---|---|
| `var_history` | `(name, frame?)` | Ordered list of `{step, line, old, new}` for a variable |
| `first_change_where` | `(name, predicate)` | First step where the new value satisfies e.g. `< 0` |
| `stack_at` | `(step)` | Call stack (function + line) at a step |
| `locals_at` | `(step, frame?)` | Reconstructed variable values at a step |
| `diff_between` | `(step_a, step_b)` | All variables that changed between two steps |
| `source_lines` | `(start, end)` | Source code text with line numbers |
| `get_output` | `()` | Program stdout/stderr with step tags |
| `get_exception` | `()` | Final exception, if any, with step and stack |
| `trace_back` | `(name, step, depth?)` | **Causal chain** (see below) |

### 6.2 Reconstructing state at any step

Because only diffs are stored, `locals_at(step)` replays changes up to `step` for the relevant frame:

```python
def locals_at(step, frame_id):
    rows = db.execute("""SELECT var, new FROM changes
                         WHERE frame_id=? AND step<=? ORDER BY step""",
                      (frame_id, step))
    state = {}
    for var, new in rows:
        state[var] = new
    return state
```

For long traces, store a **full checkpoint every 1,000 steps** and replay only from the nearest checkpoint.

### 6.3 `trace_back`: the causal chain (the "wow" feature)

This is a lightweight **dynamic backward slice**. Given "variable `V` has a bad value at step `S`", find *what produced it*.

**Preprocessing (once):** parse the source with `ast` and, for each line, compute:

- `writes(line)`: names assigned on that line
- `reads(line)`: names read on that line (including those in conditions and call arguments)

**Algorithm:**

```
trace_back(V, S, depth_limit):
    chain = []
    queue = [(V, S)]
    visited = set()
    while queue and len(chain) < depth_limit:
        var, step = queue.pop(0)
        w = last change to `var` with change.step <= step
        if w is None or w in visited: continue
        visited.add(w)
        chain.append(node(w.step, w.line, var, w.old, w.new))
        # What did that line READ? Those are the causes.
        for r in reads(w.line):
            if r != var or reads_self_matters:
                queue.append((r, w.step))
        # Control dependence: which `if`/loop condition guarded this line?
        for cvars in enclosing_condition_reads(w.line):
            queue.append((cvars, w.step))
    return rank_and_prune(chain)
```

**Ranking/pruning:** keep nodes whose value *changed meaningfully* (e.g. a sign flip, a jump in magnitude, or a value first appearing), and cap the chain at roughly 8 nodes so the UI stays readable. Mark the **root cause candidate** as the earliest node whose inputs were already "correct" (or the point where an invalid value first appears).

**Example output:**

```json
{
  "target": {"var": "total", "step": 412, "value": "-3.50"},
  "chain": [
    {"step": 412, "line": 38, "var": "total",    "old": "6.50", "new": "-3.50"},
    {"step": 409, "line": 31, "var": "discount", "old": "5.0",  "new": "10.0"},
    {"step": 402, "line": 27, "var": "coupons",  "old": "['SAVE5']", "new": "['SAVE5','SAVE5']"}
  ],
  "root_cause_step": 402
}
```

### 6.4 Honest limitations

This is dynamic slicing at **name level**, not full object-graph provenance. It handles locals, simple attribute writes, and mutated containers well, and may over- or under-approximate with aliasing and deep object mutation. Document this in the README; judges respect clearly stated scope.

---

## 7. The Agent (LLM Layer)

### 7.1 Responsibilities

1. Read the user's question plus a tiny context block (script name, final exception or last outputs).
2. Choose and call query tools (typically 2 to 5 calls).
3. Produce the final structured answer.

### 7.2 Loop

```python
SYSTEM = """You are Rewind, a debugging assistant. You can only learn about the
program by calling the provided tools. Never guess values. Cite step numbers.
When done, answer with JSON: {answer, chain_steps, root_cause_step, confidence}."""

def ask(question, tools, max_turns=8):
    messages = [{"role": "user", "content": question}]
    for _ in range(max_turns):
        resp = client.messages.create(
            model="claude-sonnet-5-5", max_tokens=1000,
            system=SYSTEM, tools=TOOL_SCHEMAS, messages=messages)
        if resp.stop_reason != "tool_use":
            return parse_final(resp)
        messages.append({"role": "assistant", "content": resp.content})
        results = [run_tool(b) for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": results})
    return fallback_answer()
```

### 7.3 Guardrails

- **Grounding rule:** every claim in `answer` must reference a step id that exists. The server validates this; unknown step ids cause a retry.
- **Bounded tool output:** every tool truncates results (for example, max 50 rows) so context stays small.
- **Turn cap:** 8 tool turns, then a graceful "here is what I found so far".
- **Deterministic fallback:** if the API is unavailable, still run `trace_back` on the most likely variable (from the exception or a keyword match in the question) and show the chain without narration.

### 7.4 Example conversation trace

```
User: "Why is the total negative?"
 → tool: get_output()                 # sees "Total: -3.50"
 → tool: var_history("total")         # finds last change at step 412
 → tool: trace_back("total", 412)     # gets 3-node causal chain
 → tool: source_lines(25, 40)         # fetches code for the explanation
Answer: "total went negative at line 38 because discount (10.0) exceeded
         the subtotal (6.50). discount doubled at step 409 because the
         coupon 'SAVE5' was added twice at step 402 (line 27)."
```

---

## 8. Voice Layer

Rewind is voice-first in two senses.

**Built by voice (hackathon requirement).** The whole codebase is created by dictating prompts with Wispr Flow into your AI coding tool and editor.

**Used by voice (product feature).** The UI has a prominent question box that is focused on load. Wispr Flow dictates into any text field, so the user presses the Wispr hotkey, speaks the question, and presses Enter. Optionally add a built-in mic button using the browser **Web Speech API** (`SpeechRecognition`) as a fallback for users without Wispr.

```
[ 🎙  Why is the total negative?                     ] [Ask]
```

---

## 9. Server and API

FastAPI app started by `rewind serve trace.db`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve the single-page UI |
| `GET` | `/api/meta` | Script name, step count, final status |
| `GET` | `/api/source` | Full source text with line numbers |
| `GET` | `/api/step/{n}` | Line, stack, and locals at step `n` |
| `GET` | `/api/range?from=&to=` | Compact steps for the scrubber |
| `POST` | `/api/ask` | Body `{question}`; returns `{answer, chain, root_cause_step}` |
| `WS` | `/ws/ask` | Optional streaming of tool calls as they happen |

Streaming the agent's tool calls to the UI ("Checking history of `total`…") makes the wait feel intentional and looks great in a demo.

---

## 10. Frontend UI

### 10.1 Layout

```
┌──────────────────────────────────────────────────────────────┐
│ 🎙 [ Ask: Why is the total negative?                 ] [Ask] │
├────────────────────────────┬─────────────────────────────────┤
│  Source code               │  Answer                         │
│  25  coupons = []          │  total went negative at line 38 │
│  27  coupons.append(c) ◀──┐│  because ...                    │
│  31  discount = calc(...) ◀┤│                                 │
│  38  total = sub - discount◀┘│  Causal chain                  │
│                            │  ① step 402  coupons ...        │
│                            │  ② step 409  discount ...       │
│                            │  ③ step 412  total ...          │
├────────────────────────────┴─────────────────────────────────┤
│ Timeline  ──●──────────●───●────────────────────────  step 402│
│ Variables @ step: coupons=['SAVE5','SAVE5']  discount=5.0 ... │
└──────────────────────────────────────────────────────────────┘
```

### 10.2 Behavior

- **Scrubber** moves through steps; the code view highlights the current line and the variables panel updates (changed variables flash).
- **Chain highlight:** chain lines get colored gutters with numbered badges; clicking a badge jumps the scrubber to that step.
- **Root cause jump:** after an answer arrives, the UI animates to `root_cause_step` automatically. This is the demo's wow moment.
- **Timeline markers:** small ticks on the scrubber for the chain steps, the exception, and relevant prints.
- **Theme:** supports light and dark mode.

### 10.3 Tech choice

Single `index.html` with vanilla JS (plus a small syntax-highlighting library such as Prism or highlight.js from a CDN). No build tooling means fewer failure points and less to dictate.

---

## 11. Repository Layout

```
rewind/
├── README.md
├── pyproject.toml               # `pip install -e .` gives the `rewind` command
├── rewind/
│   ├── __init__.py
│   ├── cli.py                   # `rewind run`, `rewind serve`, `rewind ask`
│   ├── recorder.py              # sys.settrace recorder
│   ├── store.py                 # SQLite schema + batched writer
│   ├── analysis.py              # ast read/write sets, control deps
│   ├── engine.py                # query tools incl. trace_back
│   ├── agent.py                 # LLM tool loop + validation
│   ├── server.py                # FastAPI app
│   └── static/index.html        # UI
├── demos/
│   ├── cart_negative_total.py   # coupon applied twice
│   ├── off_by_one_pagination.py # last page missing items
│   └── mutable_default_arg.py   # classic shared-list bug
├── tests/
│   ├── test_recorder.py
│   ├── test_engine.py
│   └── test_trace_back.py
└── docs/architecture.md
```

### CLI

```bash
rewind run demos/cart_negative_total.py      # records -> trace.db
rewind serve trace.db                        # opens the UI
rewind ask trace.db "Why is the total negative?"   # terminal answer, no UI
```

---

## 12. Demo Bugs (decide the quality of your demo)

| Demo | Bug | Question to ask | Expected chain |
|---|---|---|---|
| `cart_negative_total` | Coupon appended twice, discount exceeds subtotal | "Why is the total negative?" | coupons → discount → total |
| `off_by_one_pagination` | `range(0, n // size)` drops the last partial page | "Why are 3 items missing from page output?" | n // size → pages → items |
| `mutable_default_arg` | `def add(x, bucket=[])` shares state across calls | "Why does the second user see the first user's data?" | bucket identity → shared append |

Each demo should be under 60 lines, run in under a second, and have **one** clear root cause.

---

## 13. Testing Strategy

Test in layers so you always know which layer broke.

1. **Recorder unit tests:** run tiny scripts and assert the exact `changes` rows (this catches the off-by-one attribution).
2. **Engine tests (no LLM):** assert `var_history`, `locals_at`, and `trace_back` outputs on the three demo programs.
3. **Agent tests with a fake LLM:** a stub that returns scripted tool calls, to test the loop, validation, and fallback.
4. **Golden demo test:** for each demo, assert that `root_cause_step` points to the known buggy line.
5. **Manual live test:** run the real model on all three demos at least five times and note flaky answers.

---

## 14. Performance and Safety

| Concern | Mitigation |
|---|---|
| Tracing slows the program (often 10 to 50x) | Trace only the target file; step cap; document as a debugging tool |
| Large objects in `repr` | Truncate to about 200 chars; skip huge containers after a size check |
| Trace size | Diff-only storage; batched inserts; optional zlib on values |
| Running untrusted code | Rewind executes the target script with your privileges, so only run code you trust (state this in the README) |
| API key handling | Read `ANTHROPIC_API_KEY` from the environment; never write it into the trace |
| LLM hallucination | Tool-only grounding; server validates cited step ids |

---

## 15. Build Plan (deadline: October 10)

| Day | Goal | Exit criterion |
|---|---|---|
| **Oct 6** | Wispr account via referral link, start recording, scaffold repo, build `recorder.py` and `store.py` | `rewind run` produces a correct `trace.db` for a 20-line script |
| **Oct 7** | `engine.py` tools, `analysis.py`, `trace_back` | All three demo bugs give the correct chain **without** the LLM |
| **Oct 8** | `agent.py`, `server.py`, CLI `ask` | `rewind ask` answers all three demo questions correctly |
| **Oct 9** | UI: scrubber, highlight, root-cause jump; README; tests | Full flow works in the browser |
| **Oct 10 AM** | Record the final demo, push to GitHub, submit the form by the afternoon | Submission confirmed (no resubmissions allowed) |

### Hackathon-rule checklist

- [ ] Wispr Flow account created through `https://ref.wisprflow.ai/hhg`
- [ ] Every build session screen-recorded with the Wispr overlay visible
- [ ] Code, commit messages, and README written by voice
- [ ] Public GitHub repo with README and install steps
- [ ] Demo video showing the *development process*, not just the result
- [ ] Form submitted: `https://forms.gle/Lv9wF8gYVHdEqfJW8`

---

## 16. Demo Video Script (about 2 to 3 minutes)

| Time | Content |
|---|---|
| 0:00 to 0:15 | Show the Wispr account, state the idea in one sentence |
| 0:15 to 1:00 | Sped-up montage of voice-building: dictated prompts, the overlay, code appearing |
| 1:00 to 1:20 | Show the buggy cart program printing a negative total |
| 1:20 to 2:00 | `rewind run`, then `rewind serve`; say the question; UI jumps to the root cause with the chain highlighted |
| 2:00 to 2:30 | Show a second bug quickly (pagination), then the architecture slide |
| 2:30 to 2:45 | Close: *"Built entirely by voice, and you debug with your voice too."* |

---

## 17. Stretch Goals (only after the core is solid)

1. **Auto-explain crashes:** if the program raises an exception, answer "why did it crash?" automatically on load.
2. **Fix it button:** the LLM proposes a patch, Rewind re-runs it, and shows the passing run.
3. **Run diff:** record a passing and a failing run, then ask "why did this run differ?"
4. **VS Code extension:** open the Rewind panel next to the editor.
5. **Spoken answers:** read the explanation aloud with the browser `speechSynthesis` API.

---

## 18. One-Paragraph Pitch (for the form and README)

> **Rewind** is a time-travel debugger you talk to. It records a Python program's full execution, then lets you ask "why?" by voice. A deterministic query engine computes the causal chain of line executions and variable changes behind any value, and an LLM agent narrates it, so answers are grounded in what actually happened rather than guessed from source code. The entire project was built by voice with Wispr Flow, and it is used by voice too.
