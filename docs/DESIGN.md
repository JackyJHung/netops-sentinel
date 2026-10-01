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

## Milestone 3: correlation and RCA that survive the hard scenario

All choices in this milestone were made on the tuning split. The test split was run once at the end, and nothing was changed after seeing it.

### D14. Score RCA per fault with a filtered rank; measure merges separately

- **Metric.** For each detected fault, take the incident that carries its evidence, remove the roots of *other* faults evidenced in that incident from the candidate list, and take the rank of this fault's root ("filtered rank", as in knowledge-graph link prediction). An incident that ranks two concurrent roots first and second scores top-1 for both. Declaring extra roots does not help, because the metric only reads the ranking.
- **The metric alone moves the number a lot.** Same milestone 2 pipeline, hard scenario, test: top-1 0.62 under the old metric (one root per incident, so a merged pair always loses one fault) vs 0.84 under the filtered rank. Every before/after table below uses the new metric on both sides.
- **Filtered rank is lenient about merging**, so merging gets its own number: *wrong merge* = share of concurrent pairs on unrelated branches (no dependency path) judged on the same incident. Same-branch merges are reported separately and are not counted as errors (a switch fault and a fault on a service behind it are reasonably one incident, if both roots are named).
- **Extra-root precision** keeps multi-root honest: of the second and third roots an incident declares, the share that are roots of faults active at the time.
- **Judged incident.** A fault is now judged on the incident with the most evidence of it (an alert or silence on its root first, then alerts in its blast radius), not the biggest incident. Found because `scripts/diagnose.py` (with its own copy of the old rule) disagreed with the benchmark on two silent-root faults: the pipeline had correctly built a separate incident rooted at the silent switch, but the biggest incident was the concurrent partner's. The diagnostic now reads per-fault outcomes from `score_day`, so the two cannot drift again.

### D15. Split correlated groups at their origins

- **Problem.** Union-find over "overlaps in time and topologically related" is transitive, and nearly every node feeds `web-1`, so 92% of unrelated concurrent pairs were merged (test).
- **Algorithm.** In each group, origins are nodes with no upstream node that alerted (or went silent) before them, allowing `ORIGIN_TOL` = 2 min of jitter. Origins stay together if one depends on the other or they started within `split_gap` minutes; otherwise they become separate incidents. Every other alert goes to the upstream origin cluster whose onset most recently preceded it (a shared service that alerts at 14:03 belongs to the fault that started at 14:01, not the one at 13:40).
- **Tuning `split_gap` (tune, hard, full pipeline).** Only 13 unrelated concurrent pairs on the tuning split, so this is a small sample:

| split_gap | 0 | 1 | 2 | 3 | 5 | 8 |
|---|---|---|---|---|---|---|
| wrong merge | 0.00 | 0.00 | 0.08 | 0.08 | 0.23 | 0.69 |
| precision | 0.743 | 0.743 | 0.741 | 0.741 | 0.737 | 0.724 |

  Kept 1 min: same result as 0, and it tolerates a one-minute polling offset between two views of the same event. The difference between 1 and 3 is one pair, so the choice is a judgment call more than a measured optimum. Test split has 64 unrelated pairs; wrong merges there: 0.92 before, 0.06 after (0.20 with splitting alone).
- **Clean cost.** On tune, clean is unchanged. On test, clean precision drops 0.91 to 0.90: splitting separates 6 log-burst false alarms that had been hidden inside real incidents (26 to 32 false-positive incidents). They were always false alarms; now they are counted.

### D16. A node that goes silent is evidence

- `detect_silences`: every metric of a node missing for at least 5 min (a single missing series is a gap, not a silence; four independent series rarely go missing together by chance).
- A silence is attached to an incident if its node alerted in the group or is upstream of an alerting node, and it started between 15 min before and 3 min after that node's first alert. Attached silences are RCA candidates (onset = silence start) and origins for splitting, so a dark switch holds its children in one incident instead of letting them split into two.
- **Numbers (test, hard).** Top-1 0.85 to 0.94, top-3 0.91 to 1.00, wrong merges 0.20 to 0.06.
- **RCA jitter fix.** One miss on tune was a silent switch that went dark 1 min *after* its child's first alert (polling delay), so the child was not penalised as "depends on an earlier-alerting node". RCA now uses the same `ORIGIN_TOL` as correlation.
- **Risk.** A random blackout of an upstream node that happens to start within that window around an unrelated incident would be credited as a root. Not observed on tune; worth watching with real data.

### D17. Multiple root causes per incident

- After ranking, the top candidate is declared a root; a later candidate is also declared (if it scores at least 40% of the top one) when the declared roots cannot explain it: it is on an unrelated branch, or it is downstream but shows CPU or memory symptoms, which do not cascade to dependents. Declared roots are listed first, and each gets its own runbook.
- **Numbers (test, hard).** Runbook match 0.75 to 0.83; top-1 0.94 to 0.96. Extra-root precision 43 of 46.
- **Remaining misses (tune, 3 of 137).** All same-branch concurrent faults where the downstream fault only has cascading symptoms (latency, errors), for example a latency fault on `db-1` during a CPU fault on `core-rtr-1`. The root is ranked 2nd or 3rd, so top-3 is still 1.00. Tried reasoning about late onsets ("a downstream node that starts alerting 14 min after its upstream root must be a new fault") on paper and rejected it: memory-leak cascades legitimately arrive late, so it would declare false roots on every leak.

### D18. Milestone 3 before and after (test split)

| Hard scenario | Precision | RCA top-1 | RCA top-3 | Runbook match | Wrong merges | MTTD (min) |
|---|---|---|---|---|---|---|
| Milestone 2 pipeline, old metric | 0.72 | 0.62 | 0.91 | 0.70 | n/a | 1.7 |
| Milestone 2 pipeline, per-fault metric | 0.72 [0.68, 0.77] | 0.84 [0.80, 0.88] | 0.91 [0.88, 0.94] | 0.70 [0.66, 0.74] | 0.92 [0.84, 0.99] | 1.7 |
| + incident splitting | 0.75 [0.71, 0.79] | 0.85 [0.81, 0.89] | 0.91 [0.88, 0.94] | 0.78 [0.75, 0.82] | 0.20 [0.11, 0.30] | 1.8 |
| + silent-node evidence | 0.76 [0.71, 0.80] | 0.94 [0.92, 0.97] | 1.00 [1.00, 1.00] | 0.75 [0.71, 0.80] | 0.06 [0.01, 0.12] | 1.8 |
| + multi-root RCA | 0.76 [0.71, 0.80] | 0.96 [0.94, 0.98] | 1.00 [1.00, 1.00] | 0.83 [0.79, 0.87] | 0.06 [0.01, 0.12] | 1.8 |

| Clean scenario | Precision | Recall | RCA top-1 | Runbook match |
|---|---|---|---|---|
| Before | 0.91 [0.89, 0.94] | 1.00 | 1.00 | 1.00 |
| After | 0.90 [0.87, 0.93] | 1.00 | 1.00 | 1.00 |

- MTTD rises 1.7 to 1.8 min (within the CI): when a group is split, an early alert in a fault's blast radius can land in the partner's incident, which no longer counts as this fault's first page.
- Runbook match dips when silent nodes are added (0.78 to 0.75) and recovers with multi-root. Checked on tune: the 3 faults that lose their runbook all get `RB-GENERIC`, because a silent root has no alerts to match a runbook on. One is the fault's own silent root; in the other two, a concurrent silent root is the incident's only root until multi-root gives the second fault its own runbook. Worth fixing by inferring the runbook for a silent network device from its children's symptoms.

## Milestone 4: log-burst false positives

### D19. Diagnosis: one template, one rule, a multiple-testing problem

`scripts/diagnose_logs.py` labels every log alert as fault-related, benign-related, or false, by template and trigger. Tuning split, full milestone 3 pipeline:

| Template | Trigger | Fault-related | False | False but "corroborated" by a metric alert |
|---|---|---|---|---|
| `WARN retrying connection to metrics-exporter attempt=<NUM>` | burst of known template | 3 clean / 3 hard | 6 clean / 6 hard | 0 clean / 2 hard |
| 11 other templates (UPDOWN, CPUHOG, OOM, GC pause, slow query, upstream timeout, ...) | new event type | all | 0 | n/a |

- Every false log alert came from one routine template through one rule. On test the pattern is identical: 35 (clean) and 33 (hard) false alerts, all this template, all "burst". Even its "fault-related" alerts are coincidence: a chance burst on a node that happens to be in a fault's blast radius.
- **Root cause.** The burst rule fired at `max(3, 5 x baseline)` lines per 5 min. This template runs at 0.08 to 0.39 lines per 5 min, so the floor of 3 binds. P(X >= 3) at a rate of 0.2 is about 0.1%, but the test re-runs every minute on every (node, template) series, about 1,440 times a day each, so chance bursts are expected every few days per series. The threshold ignored both the template's own variance and the number of tests.
- **What log mining is actually needed for** (same script): detecting one link flap per scenario that is only visible through `%LINK-3-UPDOWN`, and the right runbook for 23 hard-scenario faults and 2 clean ones on the tuning split (OOM, GC pause, CPUHOG, UPDOWN, slow query, worker pool saturated). All of those are "new event type" alerts, so the fix must not touch that path.

### D20. Fix: a burst must also be improbable, at a fixed false-alarm budget

- A known template now bursts only if its 5-min count also reaches the smallest k with P(X >= k) <= alpha, X ~ Poisson(template's warm-up rate), alpha = budget / 1,440 tests per day. The old size requirement (at least 3, at least 5x baseline) stays: significance and effect size, because real logs are burstier than Poisson.
- **Budget chosen up front, not tuned:** 0.01 false bursts per (node, template) per day, about one per series per quarter. The result does not depend on it: 0.1, 0.01, and 0.001 give identical tuning-split results (the chance bursts are far below any of them).
- A 30-day synthetic stream of this template (unit test) gets 4 chance bursts under the old rule and at most 1 under the new one; a real burst of 15 lines in 5 min still alerts.
- **Tried: metric corroboration** (a burst only pages if a metric alert on the same or a dependency-related node overlaps it, +-10 min). Same precision on tune. Rejected: it couples the log path to the metric path, it would suppress a real log-only burst, and it keeps false bursts that coincide with real faults (2 of 6 in the hard scenario). Calibration plus corroboration gave the same numbers as calibration alone.
- **Not tried: per-template seasonality.** The simulator's log rates are flat, so there is no seasonality to model and no way to measure the benefit here. It would matter on real data (batch jobs, business hours).

### D21. The one RCA hit calibration "lost" was luck, and it exposed a fragility

- Tune, hard: RCA top-1 0.978 to 0.971 (one fault). Traced: a chance `WARN retrying` burst on `web-1` sat inside a merged incident (core router CPU fault plus a `cache-1` memory leak). Because `web-1` is downstream of `cache-1`, that false alert raised `cache-1`'s "explains other alerts" score over the 40% floor for declaring a second root. Without the false alert, `cache-1` is not declared, even though its memory symptoms cannot come from the router.
- So the calibrated rule is right, and the declaration rule is fragile: a candidate with local (non-cascading) symptoms should not need downstream alerts to clear the score floor. Left as is in this milestone so its numbers stay attributable; it is a candidate follow-up with the extra-root precision metric as the guard.

### D22. Milestone 4 before and after (test split)

| | Precision | Recall | MTTD (min) | RCA top-1 | Runbook match | False-positive incidents |
|---|---|---|---|---|---|---|
| Clean, before | 0.90 [0.87, 0.93] | 1.00 | 2.0 | 1.00 | 1.00 | 32 of 311 |
| Clean, after | 1.00 [1.00, 1.00] | 1.00 | 2.0 | 1.00 | 1.00 | 0 of 275 |
| Hard, before | 0.76 [0.71, 0.80] | 0.99 | 1.8 | 0.96 | 0.83 | 109 of 448 |
| Hard, after | 0.80 [0.76, 0.84] | 0.99 | 1.8 | 0.97 | 0.83 | 83 of 417 |

- The 9-point precision cost of log mining on clean days is fully recovered, while keeping its recall and runbook gains over the metric-only pipeline.
- Hard scenario: all 83 remaining false-positive incidents are benign events (one per benign event). That is milestone 5's problem.

## Milestone 5: change-event correlation

### D23. A change log in the simulator, with ground truth the pipeline never reads

- `ChangeEvent(kind, node, t)`: deploys on services, config pushes on network devices, on its own RNG stream (clean scenario unchanged, faults unchanged). Hard scenario: 40% of faults are caused by a change on their root 1-10 min before they start; 80% of benign events are in the log (timestamps +-2 min, because planned work is not always logged and clocks are not aligned); about 4 harmless changes a day land on random nodes at random times, so some sit next to unrelated incidents. `caused` holds the ground truth (fault or benign id) and is stripped from everything the pipeline and API incident views see.
- **Caveat to say out loud.** The simulator decides the change-to-fault delay (1-10 min), and the lookback below is tuned against data from that simulator. Real delays have a long tail (a deploy that leaks memory may take hours), so the lookback would have to be re-fit on real change and incident history.

### D24. "Changed shortly before it alerted" as an RCA signal

- Changes on an incident's nodes in the `change_lookback` minutes before that node's first alert (or silence), plus 2 min of clock slack, are attached to the incident. In RCA they add `change_weight` to the node's score, count as local evidence for declaring a second root (like CPU or memory symptoms, a change to a node cannot come from upstream), and are named in the summary: "Recent change: config push to dist-sw-2 at 14:02, 3 min before first alert."
- **Sweep (tune, hard; 48 of 137 detected faults followed a change).** Defaults (15 min, 0.25) were set before the sweep and sit on its plateau:

| lookback / weight | top-1 overall | top-1, change-caused | top-1, other | runbook | extra-root precision |
|---|---|---|---|---|---|
| no change signal | 0.971 | 0.936 | 0.989 | 0.810 | 1.00 |
| 5 / 0.25 | 0.978 | 0.957 | 0.989 | 0.825 | 1.00 |
| 10 or 15 / 0.25 | 0.993 | 1.000 | 0.989 | 0.847 | 1.00 |
| 15 / 0.5 | 0.985 | 1.000 | 0.978 | 0.832 | 1.00 |
| 30 / 0.25 | 0.985 | 0.979 | 0.989 | 0.847 | 0.96 |

  Too much weight lets harmless changes outrank real evidence; too long a lookback attaches unrelated changes and declares false extra roots.
- The signal is binary (changed in the window or not). A version that decays with time since the change was not tried; with 48 change-caused faults on tune there is little room to measure the difference.

### D25. Milestone 5 results (test split, hard scenario)

| | RCA top-1 | top-1, 158 change-caused faults | top-1, other faults | Runbook match | Runbook, change-caused | Runbook, other | Extra-root precision |
|---|---|---|---|---|---|---|---|
| Before | 0.97 [0.95, 0.98] | 0.95 | 0.976 | 0.835 | 0.834 | 0.835 | 0.98 (43 of 44) |
| After | 0.98 [0.96, 0.99] | 0.99 | 0.972 | 0.835 | 0.873 | 0.811 | 0.92 (45 of 49) |

- **It helps where it should:** the faults that followed a change on their root are ranked first 99% of the time (from 95%) and get the right runbook more often.
- **The held-out split shows costs the tuning split did not.** Harmless changes next to incidents add about 3 false extra roots and pull the top root (and its runbook) to the wrong node for a few faults that had nothing to do with a change, so net runbook match is flat. On tune, none of these costs appeared (extra-root precision 1.00, net runbook +3.7 points). Nothing was re-tuned after seeing this. Honest reading: a clear win for change-caused faults, a small and real price in confounders, overall top-1 up one point with overlapping CIs.
- Precision and recall do not move: the signal re-ranks causes, it does not page or suppress anything.

### D26. Not done: change-aware paging

- Benign events are the only false positives left (83 of 83 on test), and 80% of them are in the change log. The tempting rule, "suppress alerts right after a recorded change", is wrong: 40% of faults are also right after a change, and suppressing them is the worst possible outcome. The batch evaluation also knows how long each anomaly lasted, which a live pager does not.
- A defensible version: when a node alerts within a few minutes after a recorded change, hold the page for a short grace period and drop it if the anomaly clears (a rolling restart recovers in minutes, a bad deploy does not), paging with the change cited if it persists. The cost is MTTD on change-caused faults (plus the grace period), so it needs its own MTTD-vs-precision curve and a streaming evaluation to be measured honestly. Left as the first roadmap item.

## Follow-up: change-aware paging

### D27. Hold a page after a recorded change; drop it if it clears

- **Rule** (`paging.py`). If a recorded change hit any of an incident's nodes (alerting or silent) in the 15 min before the incident started (the RCA change window, plus 2 min of clock slack), hold the page for `change_hold` minutes. If every alert has ended by then, suppress the incident (listed, not paged); otherwise page when the hold expires and say which change it followed. It only uses what is known when the hold expires, so a live pager could do the same. Evaluation counts only paged incidents, and MTTD for a held incident is measured to the page.
- **Why not "suppress after changes".** 40% of faults follow a change; blanket suppression would hide exactly the outages that changes cause. Duration is the difference: a rolling restart recovers in minutes, a bad deploy does not.
- **Criterion set before the sweep:** the smallest hold that captures most of the precision gain with no recall loss. **Sweep (tune, hard):**

| hold (min) | precision | recall | benign paged | MTTD, change-caused | MTTD, other | real faults suppressed |
|---|---|---|---|---|---|---|
| 0 | 0.765 | 0.993 | 1.00 | 2.2 | 1.8 | 0 |
| 5 | 0.840 | 0.986 | 0.61 | 5.9 | 2.1 | 1 |
| 8 | 0.926 | 0.986 | 0.26 | 8.2 | 2.4 | 1 |
| 10 | 0.925 | 0.978 | 0.26 | 9.7 | 2.7 | 2 |
| 15 | 0.925 | 0.978 | 0.26 | 13.9 | 3.3 | 2 |
| 20 | 0.923 | 0.949 | 0.26 | 18.0 | 3.8 | 5 |

  No hold meets "no recall loss" strictly: even 5 min suppresses one real fault. 5 and 8 lose the same fault, and 8 gets nearly all the precision, so 8 was kept; longer holds only add delay and lose faults. Clean scenario: identical at every setting (no changes there).
- **The suppressed real fault (tune).** A subtle (intensity 0.33) link flap on `core-rtr-1` caused by a config push, whose alerts lasted 5 min. To a duration rule it is indistinguishable from a benign blip; that is the inherent risk, and the reason the hold should stay short.
- **Why faults that were not caused by a change also slow down (tune).** 10 of 11 such delays come from sharing an incident with a concurrent partner fault that was change-caused, so the merged incident is held as a whole. Holding per root cause instead of per incident would fix that.
- **Results (test, hard).** Precision 0.80 [0.76, 0.84] to 0.96 [0.95, 0.98]; benign events paged 1.00 to 0.14 (mostly the ~20% of benign events never logged as changes); recall 0.99 unchanged, 0 of 71 suppressed incidents was a real fault. MTTD 1.8 to 4.7 min overall: 1.7 to 8.1 for change-caused faults, 1.8 to 2.6 for the rest. Clean scenario unchanged.
- **Is it worth it?** A policy call, not a measurement: 16 points of precision (71 fewer false pages over 30 simulated days, about 2.4 a day) against about 6 extra minutes before a change-caused outage pages. Kept on by default as the last ablation row so both numbers are visible; `PipelineConfig(change_hold=0)` turns it off.

## Where things stand (test split, full pipeline)

| | Start of session (seeds 0-9, what the README claimed) | Clean scenario now | Hard scenario now |
|---|---|---|---|
| Precision | 0.94 | 1.00 [1.00, 1.00] | 0.96 [0.95, 0.98] |
| Recall | 1.00 | 1.00 | 0.99 [0.99, 1.00] |
| MTTD (min) | 1.97 | 2.0 [1.6, 2.3] | 4.7 [4.3, 5.1] (1.8 without change-aware paging) |
| RCA top-1 | 1.00 (faults never overlapped) | 1.00 | 0.98 [0.96, 0.99] |
| Runbook match | 0.99 | 1.00 | 0.83 [0.79, 0.88] |
| Wrong merges / benign paged | not measured | n/a | 0.06 / 0.14 |

The milestone 2 baseline on the same hard days was precision 0.72, RCA top-1 0.62 under the old metric (0.84 per-fault), runbook 0.70, wrong merges 0.92.
