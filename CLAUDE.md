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

- `src/sentinel/simulator.py`: telemetry, syslog, fault injection (`Fault` has kind, root, start, duration, intensity); `SCENARIOS` (clean, hard): concurrent faults, NaN gaps/blackouts, benign events, change log (`ChangeEvent.caused` is ground truth the pipeline must never read), each on its own RNG stream
- `src/sentinel/detection.py`: static, robust z + EWMA (must agree), Isolation Forest on residuals, saturation forecast -> `Alert`
- `src/sentinel/logs.py`: Drain-style template miner, new-event-type and burst alerts; bursts must pass a Poisson tail test at a daily false-alarm budget
- `src/sentinel/correlation.py`: union-find grouping by time window + topology, then split at origins (unrelated branches, separate onsets); `detect_silences` for nodes that went dark -> `Incident`
- `src/sentinel/rca.py`: explain / earliness / intensity / recent-change scoring over alerting and silent nodes; `attach_changes`; declares multiple roots (unrelated branch, or local evidence: CPU/memory symptoms or a recent change)
- `src/sentinel/runbooks.py` + `data/runbooks.yaml`: runbook matching and summaries
- `src/sentinel/paging.py`: change-aware paging (hold a page after a recorded change, suppress it if it clears), decided per declared root; evaluation counts only paged incidents and measures MTTD to the page
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

## Current state (Oct 1, 2026)

- Milestones 1-5 done, plus change-aware paging decided per root cause (DESIGN D27, D28).
- Test split, full pipeline. Clean: P 1.00 / R 1.00 / MTTD 2.0 / RCA top-1 1.00 / runbook 1.00. Hard: P 0.96 / R 0.99 / MTTD 4.2 (7.4 for change-caused faults, 2.2 others; 1.8 without the hold) / RCA top-1 0.98 / top-3 1.00 / runbook 0.83 / wrong merges 0.06 / benign paged 0.14.
- RCA is scored per fault with a filtered rank (DESIGN D14).
- Known weaknesses: the change hold delays change-caused faults ~6 min (a partner fault whose root is not declared still waits); it suppressed one subtle real fault on tune; unlogged benign changes still page; harmless changes near incidents cost some extra-root precision (D25); multi-root declaration score floor (D21); silent roots get a generic runbook; EWMA absorbs slow ramps (D5); Poisson log rates; change lookback fitted to the simulator's own delay; small sample of unrelated concurrent pairs.

## Next steps (roadmap order)

1. Let local evidence (CPU/memory, a recent change) declare a root without the score floor (D21), guarded by extra-root precision
2. Infer a silent network device's runbook from its children's symptoms
3. Streaming mode (Kafka or Redis Streams) with online detectors
4. Prometheus / OpenTelemetry ingestion and a real change feed
5. LLM-drafted incident summaries grounded in the alert timeline
