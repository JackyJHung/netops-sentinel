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
- `src/sentinel/logs.py`: Drain-style template miner, new-event-type and burst alerts; bursts must pass a Poisson tail test at a daily false-alarm budget
- `src/sentinel/correlation.py`: union-find grouping by time window + topology, then split at origins (unrelated branches, separate onsets); `detect_silences` for nodes that went dark -> `Incident`
- `src/sentinel/rca.py`: explain / earliness / intensity scoring over alerting and silent nodes; declares multiple roots (unrelated branch, or local CPU/memory symptoms)
- `src/sentinel/runbooks.py` + `data/runbooks.yaml`: runbook matching and summaries
- `src/sentinel/evaluation.py`: seed splits, per-day scoring, pooled metrics, day-level bootstrap CIs, per-kind/intensity breakdowns, ablation ladder, parallel runner
- `src/sentinel/api.py`, `static/index.html`: API and dashboard
- `data/topology.yaml`: 10-node dependency graph (`depends_on` points upstream)
- `docs/DESIGN.md`: decision log (what was tried, numbers, what was kept and why)
- `scripts/diagnose.py`: attribute false-positive incidents, RCA misses, and wrong merges to causes (tune split by default)
- `scripts/diagnose_logs.py`: label every log alert (fault / benign / false) by template and trigger; which faults need log mining
- `scripts/readme_tables.py --update-readme`: regenerate the README results tables from `reports/benchmark-test.json` (never type numbers by hand)

## Conventions

- Time is an integer minute index everywhere; convert to timestamps only at the edges (API, CLI, logs).
- Tune only on the tuning split (`sentinel eval --split tune`). Run the test split only to report, never to pick thresholds.
- Every detector/RCA change must be justified by the tuning-split benchmark; update the README results table (test split) when numbers change, and add a `docs/DESIGN.md` entry for significant choices.
- Keep the simulator deterministic per seed. New randomness goes on a separate stream (`default_rng([seed, k])`) so the clean scenario stays byte-identical.
- Missing telemetry is NaN; detectors treat it as no evidence (never anomalous, never learned).
- README prose: no em dashes.

## Current state (Sep 30, 2026)

- Milestones 1-4 done: honest evaluation, hard scenario, correlation/RCA for concurrent faults and silent nodes, calibrated log bursts.
- Test split, full pipeline. Clean: P 1.00 / R 1.00 / MTTD 2.0 / RCA top-1 1.00 / runbook 1.00. Hard: P 0.80 / R 0.99 / MTTD 1.8 / RCA top-1 0.97 / top-3 1.00 / runbook 0.83 / wrong merges 0.06 / benign paged 1.00.
- RCA is scored per fault with a filtered rank (DESIGN D14).
- Every remaining false positive (hard) is a benign event: 83 of 83 on test.
- Known weaknesses: benign changes always page; multi-root declaration needs downstream alerts to clear its score floor even with local CPU/memory symptoms (DESIGN D21); same-branch concurrent faults with only cascading symptoms rank 2nd-3rd; silent roots get a generic runbook; EWMA absorbs slow ramps (D5); Poisson assumption for log rates; small sample of unrelated concurrent pairs (13 tune, 64 test).

## Next steps (roadmap order)

1. Change-event (deploy/config push) correlation as an RCA signal (milestone 5)
2. Streaming mode (Kafka or Redis Streams) with online detectors
3. Prometheus / OpenTelemetry ingestion
4. LLM-drafted incident summaries grounded in the alert timeline
