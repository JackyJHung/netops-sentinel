# NetOps Sentinel

AIOps portfolio project: synthetic network/service telemetry -> anomaly detection -> alert correlation -> root-cause ranking -> runbook recommendation, all scored against labeled ground truth.

## Commands

```bash
pip install -e ".[dev]"      # Python >= 3.10
pytest -q                    # ~40 tests, ~20 s
ruff check src tests         # lint (line length 130)
sentinel run --seed 42 -v    # one simulated day, prints incidents + score
sentinel eval --split tune   # ablation ladder on tuning seeds 0-9 (development) -> reports/benchmark-tune.*
sentinel eval                # held-out test seeds 100-129 (reporting only) -> reports/benchmark-test.*
sentinel serve               # FastAPI + dashboard on :8000 (/docs for OpenAPI)
```

## Layout

- `src/sentinel/simulator.py`: telemetry, syslog, fault injection (`Fault` has kind, root, start, duration, intensity)
- `src/sentinel/detection.py`: static, robust z + EWMA (must agree), Isolation Forest on residuals, saturation forecast -> `Alert`
- `src/sentinel/logs.py`: Drain-style template miner, new/burst log alerts
- `src/sentinel/correlation.py`: union-find grouping by time window + topology -> `Incident`
- `src/sentinel/rca.py`: explain / earliness / intensity scoring
- `src/sentinel/runbooks.py` + `data/runbooks.yaml`: runbook matching and summaries
- `src/sentinel/evaluation.py`: seed splits, per-day scoring, pooled metrics, day-level bootstrap CIs, per-kind/intensity breakdowns, ablation ladder, parallel runner
- `src/sentinel/api.py`, `static/index.html`: API and dashboard
- `data/topology.yaml`: 10-node dependency graph (`depends_on` points upstream)
- `docs/DESIGN.md`: decision log (what was tried, numbers, what was kept and why)

## Conventions

- Time is an integer minute index everywhere; convert to timestamps only at the edges (API, CLI, logs).
- Tune only on the tuning split (`sentinel eval --split tune`). Run the test split only to report, never to pick thresholds.
- Every detector/RCA change must be justified by the tuning-split benchmark; update the README results table (test split) when numbers change, and add a `docs/DESIGN.md` entry for significant choices.
- Keep the simulator deterministic per seed.
- README prose: no em dashes.

## Current state (Sep 30, 2026)

- Milestone 1 (honest evaluation) done: tune/test seed splits, pooled metrics, 95% day-level bootstrap CIs, per-kind and per-intensity breakdowns.
- Held-out test split, full pipeline: P 0.91 [0.89, 0.94] / R 1.00 / MTTD 2.0 min vs static baseline R 0.55. Tuning split: P 0.94.
- All tests, including the real FastAPI/httpx API tests, pass with real dependencies installed.
- Ruff rules are pinned in `pyproject.toml` (`E4, E7, E9, F`) because ruff 0.16 widened its defaults.
- Known weaknesses: log-burst alerts cost ~9 pts precision on test; RCA is 1.00 because faults never overlap; the EWMA chart absorbs slow ramps (leaks are only caught by the forecaster, see DESIGN D5).

## Next steps (roadmap order)

1. Harder scenarios: concurrent/overlapping faults, missing telemetry, benign maintenance/config events (milestone 2)
2. Correlation and multi-root RCA that survive those scenarios, silent nodes as evidence (milestone 3)
3. Cut log-burst false positives at the root cause (milestone 4)
4. Change-event (deploy/config push) correlation as an RCA signal (milestone 5)
5. Streaming mode (Kafka or Redis Streams) with online detectors
6. Prometheus / OpenTelemetry ingestion
7. LLM-drafted incident summaries grounded in the alert timeline
