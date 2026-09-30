# Design decisions

A running log of the choices behind NetOps Sentinel: what was tried, the numbers, what was kept, and why. Newest entries are at the bottom of each milestone. "Tune" means the tuning split (seeds 0-9); "test" means the held-out split (seeds 100-129). All numbers are pooled over the split unless stated otherwise.

## Milestone 1: honest evaluation protocol

### D1. Separate tuning and test seeds

- **Problem.** The original benchmark reported on seeds 0-9, the same days the detector thresholds were tuned on. That is training-set accuracy.
- **Decision.** Two fixed, disjoint splits: `tune` = seeds 0-9 (development, any number of looks) and `test` = seeds 100-129 (reporting only). `sentinel eval` defaults to `test`, so the README number is always the held-out one. Thresholds are only ever changed based on `tune`.
- **Numbers.** Full pipeline, tune vs test: precision 0.94 vs 0.91, recall 1.00 vs 1.00, MTTD 1.9 vs 2.0 min. The drop is almost entirely log-burst false positives, which cost 6 points of precision on tune and 9 on test. Every metric-only configuration has precision 1.00 on both.
- **Why 30 test days.** About 270 faults, roughly 65 per fault kind, so per-kind recall has a CI width around 0.1 instead of being dominated by one or two misses. More days cost linear time; 30 runs in under a minute.

### D2. Pool counts over days instead of averaging per-day metrics

- **Tried.** The old benchmark averaged each day's precision, recall, and MTTD (a macro average).
- **Problem.** A day with one slow detection weighs as much as a day with twelve fast ones, and per-kind slices have about two faults per day, so per-day ratios are often undefined (0/0).
- **Decision.** Sum the counts over all days, then divide (recall = all detected faults / all faults, MTTD = mean over all detected faults).
- **Numbers (tune).** Every metric moved by 0.01 or less except the static baseline's MTTD: 3.98 min averaged per day vs 3.1 min pooled. The per-day mean was inflated by a few days where the baseline caught only one fault, late.

### D3. Day-level (cluster) bootstrap for confidence intervals

- **Options.** A t-interval over per-day values, or a bootstrap.
- **Decision.** Percentile bootstrap that resamples whole days (2000 resamples, fixed RNG seed so reports are reproducible). Implemented as multinomial resample weights times per-day count vectors, so a full report takes milliseconds.
- **Why.** The reported metrics are ratios of pooled counts, which a t-interval over per-day ratios does not match. Faults on the same day share telemetry and log noise, so treating faults as independent would give intervals that are too narrow. The day is the unit that is actually independent.
- **Caveat.** With 30 days, a metric at exactly 1.00 gets a degenerate [1.00, 1.00] interval. That means "no miss observed in 30 days", not "cannot miss".

### D4. Break fault metrics down by kind and intensity

- Recall, MTTD, RCA, and runbook match are also reported per fault kind and per intensity bucket (subtle < 0.5, hard >= 0.5). Precision is incident-level and has no fault kind, so it is only reported overall.
- **What it exposed.** Static thresholds catch 3% of CPU saturations and 32% of memory leaks but every hard link flap. The forecaster's whole MTTD gain is on memory leaks (22 to 8 min on test), and without it leak runbook matching is 0%.

### D5. Finding: the EWMA chart absorbs slow ramps

- Traced a 40-minute memory leak (50% to 97%): robust z peaks at 40, EWMA z never passes 3.5. The EWMA mean chases the ramp, and every step inflates the variance estimate, so the chart widens as the leak grows. The baseline only freezes once z crosses the threshold, which a smooth ramp never does.
- Because robust z and EWMA must agree, `mem_pct` never alerts on a leak without the forecaster. The forecaster covers it today, so this is recorded rather than fixed. Candidate fixes: freeze the EWMA variance (not just the mean) above a lower z, or use a CUSUM for drift.

### D6. CI runs the tuning split

- GitHub Actions runs `sentinel eval --split tune --seeds 3` as a smoke test. Putting held-out numbers in front of us on every push invites tuning on them.

### D7. Benchmark speed: cache Isolation Forest scores, run seeds in parallel

- Profiling showed the Isolation Forest fit is about 90% of pipeline time (200 trees x 10 nodes, about 3 s per simulated day). Three of the five ablation configs use it on identical data.
- Each seed is simulated once and all configs run on it with a shared per-day score cache; seeds run in a process pool. Results are bit-identical: the new runner reproduces the old ablation table on seeds 0-9 exactly when the old per-day averaging is applied. Five configs on 30 days take about 35 s on 4 cores.

## Milestone 2: harder scenarios

### D8. Scenario features are `simulate()` options, each on its own RNG stream

- `overlap_prob`, `gap_rate`, `blackout_rate`, `fault_silence_prob`, and `benign_rate` all default to 0, and `SCENARIOS["hard"]` turns them all on. Each feature draws from `default_rng([seed, stream_id])` instead of the main generator.
- **Why separate streams.** Drawing from the main generator would reshuffle every later random number, so turning on gaps would change which faults happen. With separate streams: (1) the default simulation is byte-identical to before, so the clean benchmark and the old tests still mean the same thing (verified: the clean tuning report is identical to milestone 1, field for field); (2) the hard scenario has exactly the same primary faults as the clean one on each seed, plus extras, so clean vs hard is a paired comparison.

### D9. Concurrent faults

- With probability `overlap_prob`, each fault gets a partner (id suffix `b`) that starts between 10 min before and 20 min after it, on a different node: 50/50 on the same branch (a dependency path exists between the roots) or an unrelated one. Partners end before the next primary starts, so at most two faults overlap. Partner effects are applied after the primaries and add to them (two latency faults compound).
- **Bug caught by a test.** The first version allowed a +20 min start offset, but primaries can be 15 min long, so some "concurrent" partners started after their primary ended. The offset is now capped at the primary's duration minus 5, and a partner must overlap by at least 5 min.

### D10. Missing telemetry: NaN means "no evidence"

- Three kinds: short per-series gaps (Poisson, 1-10 min), random whole-node blackouts (30-90 min, not faults), and fault-induced silence (a hard link flap or CPU fault on a network device makes it unreachable for polling, with probability `fault_silence_prob`). Blackouts drop the node's logs as well as its metrics.
- Detector rules: a missing point never scores as anomalous and is never learned into a baseline. Robust z uses `nanmedian` and needs at least a quarter of its window to be real points. EWMA skips missing points. The forecaster fits least squares over the real points and needs 60% coverage. The Isolation Forest scores all-missing rows as normal. Series with no NaN take the original code paths, so clean results are unchanged.
- **Result.** On the hard scenario, zero false-positive incidents come from gaps or blackouts (diagnostic script, tune and test). The price: a node that goes silent is invisible to detection, which is the milestone 3 problem.

### D11. Benign events are labelled, and paging on them is a false positive

- Config pushes (network devices: CPU +35-50 points, higher latency, config syslog lines) and rolling restarts (services: error rate, latency, CPU), 3-8 min long, at least 30 min from any fault so the label is unambiguous. `benign paged` = share of benign events that produced an unmatched incident.
- **Baseline.** Every statistical configuration pages on 100% of benign events; static thresholds on 25%. On test, benign events are 83 of the full pipeline's 107 false-positive incidents.

### D12. Detection credit needs the fault's own evidence

- **Problem found.** In the hard scenario, static-threshold recall rose from 0.49 (clean) to 0.67 on tune. Diagnosis: 9 of its 92 "detected" faults had no alert on their root; they were credited through a concurrent fault's incident, because their blast radii share downstream services (almost everything feeds `web-1`).
- **Decision.** Two rules. Precision stays lenient: a page is a true positive if any fault explains it (blast radius, time window), because a downstream fragment of a real fault is not a false alarm. Recall, MTTD, and RCA need the fault's own evidence: an alert on its root in its window, or its root going silent because of it. MTTD is measured to the first alert in the fault's own blast radius, not the incident start, which may belong to a partner that paged earlier.
- **Numbers.** Clean results unchanged to the third decimal (all clean detections already had root evidence). Hard, tune: static recall 0.67 to 0.60; full-pipeline MTTD 1.53 to 1.91 min (it had been flattered by partners' earlier pages).
- **Remaining effect.** Static recall is still higher on hard (0.60) than clean (0.49), almost all on CPU saturation (7% to 28%). That is real, not an artifact: concurrent faults compound, so a CPU fault's latency multiplied by an upstream latency fault crosses the static "4x median latency" rule.

### D13. Milestone 2 baseline (test split, before any milestone 3 change)

| Scenario | Precision | Recall | MTTD (min) | RCA top-1 | RCA top-3 | Runbook match | Benign paged |
|---|---|---|---|---|---|---|---|
| Clean | 0.91 [0.89, 0.94] | 1.00 | 2.0 | 1.00 | 1.00 | 1.00 | n/a |
| Hard | 0.72 [0.68, 0.77] | 0.99 | 1.7 | 0.62 [0.58, 0.66] | 0.91 [0.88, 0.94] | 0.70 [0.66, 0.74] | 1.00 |

- Where the losses come from (`scripts/diagnose.py`, test, hard): 146 of 155 RCA top-1 misses are two concurrent faults merged into one incident, 7 are silent roots. 83 of 107 false-positive incidents are benign events, 24 are log-only bursts of `WARN retrying connection to metrics-exporter`.
- On the tuning split the same breakdown is 51 of 53 misses merged, and 31 of 35 false positives benign. Milestone 3 works from the tuning numbers only.
