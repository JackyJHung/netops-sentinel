"""Command-line entry point: `sentinel run | eval | serve | export`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation import benchmark, evaluate, to_markdown
from .pipeline import PipelineConfig, run_pipeline
from .simulator import simulate
from .topology import Topology


def _config(name: str) -> PipelineConfig:
    return PipelineConfig.baseline() if name == "static" else PipelineConfig()


def cmd_run(args) -> None:
    topo = Topology.default()
    sim = simulate(topo, minutes=args.minutes, seed=args.seed)
    result = run_pipeline(sim, topo, _config(args.config))
    print(f"Simulated {args.minutes} min, {len(sim.faults)} injected faults, {len(sim.logs)} log lines")
    print(f"{len(result.alerts)} alerts -> {len(result.incidents)} incidents\n")
    for inc in result.incidents:
        print(f"[{inc.incident_id}] {inc.summary}")
        if args.verbose and inc.runbook:
            for step in inc.runbook["steps"]:
                print(f"     - {step}")
    print("\nGround truth:")
    for f in sim.faults:
        print(f"  {f.fault_id} {f.kind:<20} root={f.root:<12} {sim.timestamp(f.start):%H:%M}-{sim.timestamp(f.end):%H:%M} intensity={f.intensity}")
    print("\nScore:", json.dumps(evaluate(sim, result, topo).to_dict(), indent=2))


def cmd_eval(args) -> None:
    results = benchmark(seeds=range(args.seeds), minutes=args.minutes)
    md = to_markdown(results)
    print(md)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(results, indent=2))
    (out / "benchmark.md").write_text(f"Averaged over {args.seeds} simulated days ({args.minutes} min each).\n\n{md}\n")
    print(f"\nWrote {out / 'benchmark.md'}")


def cmd_export(args) -> None:
    """Write a simulated day to disk (metrics CSV, raw logs, ground truth) for use in other tools."""
    sim = simulate(minutes=args.minutes, seed=args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    flat = sim.metrics.copy()
    flat.columns = [f"{n}.{m}" for n, m in flat.columns]
    flat.insert(0, "timestamp", [sim.timestamp(t) for t in flat.index])
    flat.to_csv(out / "metrics.csv", index=False)
    (out / "syslog.log").write_text("\n".join(ln.render(sim.start_time) for ln in sim.logs) + "\n")
    (out / "faults.json").write_text(json.dumps([f.to_dict() for f in sim.faults], indent=2))
    print(f"Wrote metrics.csv, syslog.log, faults.json to {out}")


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("sentinel.api:app", host=args.host, port=args.port, reload=False)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="sentinel", description="NetOps Sentinel: AIOps incident detection and RCA")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="simulate a day and print incidents")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--minutes", type=int, default=1440)
    r.add_argument("--config", choices=["sentinel", "static"], default="sentinel")
    r.add_argument("-v", "--verbose", action="store_true", help="print runbook steps")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("eval", help="benchmark vs static-threshold baseline")
    e.add_argument("--seeds", type=int, default=10)
    e.add_argument("--minutes", type=int, default=1440)
    e.add_argument("--out", default="reports")
    e.set_defaults(func=cmd_eval)

    x = sub.add_parser("export", help="write simulated telemetry to disk")
    x.add_argument("--seed", type=int, default=42)
    x.add_argument("--minutes", type=int, default=1440)
    x.add_argument("--out", default="data")
    x.set_defaults(func=cmd_export)

    s = sub.add_parser("serve", help="start the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
