"""CLI: rewind run | serve | ask"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    p = argparse.ArgumentParser(prog="rewind", description="Rewind: time-travel debugger you question by voice")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Record a Python program -> trace.db")
    r.add_argument("script", help="Target python file")
    r.add_argument("-o", "--out", default="trace.db")
    r.add_argument("--step-limit", type=int, default=200_000)

    s = sub.add_parser("serve", help="Serve trace.db UI")
    s.add_argument("db", nargs="?", default="trace.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    a = sub.add_parser("ask", help="Ask a question in the terminal (no UI)")
    a.add_argument("db", help="trace.db path")
    a.add_argument("question", help="e.g. 'Why is the total negative?'")

    args = p.parse_args(argv)
    if args.cmd == "run":
        from .recorder import record
        if not os.path.exists(args.script):
            print(f"not found: {args.script}", file=sys.stderr)
            raise SystemExit(2)
        st = record(args.script, args.out, step_limit=args.step_limit)
        print(f"recorded {st.get('steps',0)} steps -> {args.out}")
        if st.get("error"):
            print(f"program raised: {st['error']}")
    elif args.cmd == "serve":
        from .server import serve
        serve(args.db, host=args.host, port=args.port)
    elif args.cmd == "ask":
        from .agent import ask
        out = ask(args.question, args.db)
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
