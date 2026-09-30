# NetOps Sentinel

AIOps portfolio project: synthetic network/service telemetry -> anomaly detection -> alert correlation -> root-cause ranking -> runbook recommendation, all scored against labeled ground truth.

## Commands

```bash
pip install -e ".[dev]"      # Python >= 3.10
pytest -q                    # 28 tests
ruff check src tests         # lint (line length 130)
sentinel run --seed 42 -v    # one simulated day, prints incidents + score
sentinel eval --seeds 10     # benchmark vs static baseline -> reports/
sentinel serve               # FastAPI + dashboard on :8000 (/docs for OpenAPI)
```

## Layout

- `src/sentinel/simulator.py`: telemetry, syslog, fault injection (`Fault` has kind, root, start, duration, intensity)
- `src/sentinel/detection.py`: static, robust z + EWMA (must agree), Isolation Forest on residuals, saturation forecast -> `Alert`
- `src/sentinel/logs.py`: Drain-style template miner, new/burst log alerts
- `src/sentinel/correlation.py`: union-find grouping by time window + topology -> `Incident`
- `src/sentinel/rca.py`: explain / earliness / intensity scoring
- `src/sentinel/runbooks.py` + `data/runbooks.yaml`: runbook matching and summaries
- `src/sentinel/evaluation.py`: precision, recall, MTTD, RCA top-k, classification, compression
- `src/sentinel/api.py`, `static/index.html`: API and dashboard
- `data/topology.yaml`: 10-node dependency graph (`depends_on` points upstream)

## Conventions

- Time is an integer minute index everywhere; convert to timestamps only at the edges (API, CLI, logs).
- Every detector/RCA change must be justified by `sentinel eval`; update the README results table when numbers change.
- Keep the simulator deterministic per seed.
- README prose: no em dashes.

## Current state (Sep 30, 2026)

- 10-seed benchmark: sentinel P 0.94 / R 1.00 / MTTD 1.97 min vs static baseline R 0.49.
- API tests were only verified via a stub (PyPI was blocked in the build sandbox); CI runs the real ones.
- Known weaknesses: log-burst alerts cost ~6 pts precision; RCA is 1.00 because faults never overlap.

## Next steps (roadmap order)

1. Concurrent/overlapping faults and missing telemetry, to stress RCA honestly
2. Cut log-burst false positives
3. Streaming mode (Kafka or Redis Streams) with online detectors
4. Prometheus / OpenTelemetry ingestion
5. Change-event (deploy/config push) correlation as an RCA signal
6. LLM-drafted incident summaries grounded in the alert timeline
