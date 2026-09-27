"""Command line interface: ``fre simulate | validate | replay | serve``."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .engine import DecisionEngine
from .rules import RuleValidationError, load_ruleset
from .store import EventStore

DEFAULT_PROD = "rules/production.yaml"
DEFAULT_SHADOW = "rules/candidate.yaml"


def cmd_simulate(args: argparse.Namespace) -> int:
    from .report import run_simulation, write_summary

    result = run_simulation(
        production=args.rules,
        shadow=args.shadow or None,
        n_legit=args.players,
        seed=args.seed,
        db_path=args.db,
        analyst_error_rate=args.analyst_error,
    )
    path = write_summary(result, args.out)
    c = result.confusion
    print(
        f"events={result.n_events} players={len(result.population.labels)} "
        f"precision={c.precision:.3f} recall={c.recall:.3f} fpr={c.fpr:.4f} "
        f"replay_identical={result.replay_identical}"
    )
    print(f"summary written to {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    rc = 0
    for p in args.paths:
        try:
            rs = load_ruleset(p)
        except RuleValidationError as exc:
            print(f"INVALID {p}: {exc}")
            rc = 1
        else:
            print(f"OK {p}: {rs.label}, {len(rs.rules)} rules, checksum {rs.checksum}")
    return rc


def cmd_replay(args: argparse.Namespace) -> int:
    store = EventStore(args.db)
    decisions = DecisionEngine.replay(store, load_ruleset(args.rules))
    print(f"replayed {len(decisions)} events with {args.rules}")
    for action, n in Counter(d.action for d in decisions).most_common():
        print(f"  {action}: {n}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(db_path=args.db, production=args.rules, shadow=args.shadow or None)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fre", description="Explainable fraud rules engine (synthetic demo)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="generate synthetic population, run engine, write summary")
    s.add_argument("--players", type=int, default=2000, help="number of legit players")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--rules", default=DEFAULT_PROD)
    s.add_argument("--shadow", default=DEFAULT_SHADOW)
    s.add_argument("--db", default=":memory:")
    s.add_argument("--analyst-error", type=float, default=0.05)
    s.add_argument("--out", default="reports/summary.md")
    s.set_defaults(func=cmd_simulate)

    v = sub.add_parser("validate", help="validate rule set files")
    v.add_argument("paths", nargs="+", type=Path)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("replay", help="rebuild decisions from an event store")
    r.add_argument("--db", required=True)
    r.add_argument("--rules", default=DEFAULT_PROD)
    r.set_defaults(func=cmd_replay)

    sv = sub.add_parser("serve", help="run the FastAPI service")
    sv.add_argument("--db", default="fre.sqlite")
    sv.add_argument("--rules", default=DEFAULT_PROD)
    sv.add_argument("--shadow", default=DEFAULT_SHADOW)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
