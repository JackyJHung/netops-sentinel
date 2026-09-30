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
