# NetOps Sentinel

AIOps portfolio project: synthetic network/service telemetry -> anomaly detection -> alert correlation -> root-cause ranking -> runbook recommendation, all scored against labeled ground truth.

## Commands

```bash
pip install -e ".[dev]"      # Python >= 3.10
pytest -q                    # ~40 tests, ~20 s
ruff check src tests scripts # lint (line length 130)
sentinel run --seed 42 -v    # one simulated day, prints incidents + score
sentinel eval --split tune   # ablation ladder on tuning seeds 0-9 (development) -> reports/benchmark-tune.*
sentinel eval                # held-out test seeds 100-129 (reporting only) -> reports/benchmark-test.*
sentinel serve               # FastAPI + dashboard on :8000 (/docs for OpenAPI)
```

## Layout

- `src/sentinel/simulator.py`: telemetry, syslog, fault injection (`Fault` has kind, root, start, duration, intensity); `SCENARIOS` (clean, hard): concurrent faults, NaN gaps/blackouts, benign events, each on its own RNG stream
- `src/sentinel/detection.py`: static, robust z + EWMA (must agree), Isolation Forest on residuals, saturation forecast -> `Alert`
- `src/sentinel/logs.py`: Drain-style template miner, new/burst log alerts
- `src/sentinel/correlation.py`: union-find grouping by time window + topology -> `Incident`
- `src/sentinel/rca.py`: explain / earliness / intensity scoring
- `src/sentinel/runbooks.py` + `data/runbooks.yaml`: runbook matching and summaries
- `src/sentinel/evaluation.py`: seed splits, per-day scoring, pooled metrics, day-level bootstrap CIs, per-kind/intensity breakdowns, ablation ladder, parallel runner
- `src/sentinel/api.py`, `static/index.html`: API and dashboard
- `data/topology.yaml`: 10-node dependency graph (`depends_on` points upstream)
- `docs/DESIGN.md`: decision log (what was tried, numbers, what was kept and why)
- `scripts/diagnose.py`: attribute false-positive incidents and RCA misses to causes (tune split by default)

## Conventions

- Time is an integer minute index everywhere; convert to timestamps only at the edges (API, CLI, logs).
- Tune only on the tuning split (`sentinel eval --split tune`). Run the test split only to report, never to pick thresholds.
- Every detector/RCA change must be justified by the tuning-split benchmark; update the README results table (test split) when numbers change, and add a `docs/DESIGN.md` entry for significant choices.
- Keep the simulator deterministic per seed. New randomness goes on a separate stream (`default_rng([seed, k])`) so the clean scenario stays byte-identical.
- Missing telemetry is NaN; detectors treat it as no evidence (never anomalous, never learned).
- README prose: no em dashes.

## Current state (Sep 30, 2026)

- Milestones 1 (honest evaluation) and 2 (hard scenario) done. `sentinel eval` runs both scenarios on a split.
- Test split, full pipeline. Clean: P 0.91 / R 1.00 / MTTD 2.0 / RCA top-1 1.00. Hard: P 0.72 / R 0.99 / MTTD 1.7 / RCA top-1 0.62 / top-3 0.91 / runbook 0.70 / benign paged 1.00.
- Hard-scenario losses: RCA misses are almost all merged concurrent faults (51 of 53 on tune); false positives are mostly benign events (31 of 35 on tune), the rest one log template.
- Known weaknesses: single root per incident; silent nodes are invisible; benign changes always page; log-burst FPs from `WARN retrying connection to metrics-exporter`; EWMA absorbs slow ramps (DESIGN D5).

## Next steps (roadmap order)

1. Correlation and multi-root RCA that survive those scenarios, silent nodes as evidence (milestone 3)
2. Cut log-burst false positives at the root cause (milestone 4)
3. Change-event (deploy/config push) correlation as an RCA signal (milestone 5)
4. Streaming mode (Kafka or Redis Streams) with online detectors
5. Prometheus / OpenTelemetry ingestion
6. LLM-drafted incident summaries grounded in the alert timeline
