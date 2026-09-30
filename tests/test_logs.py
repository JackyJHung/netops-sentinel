import numpy as np

from sentinel.logs import TemplateMiner, detect_log_anomalies, mask, parse_logs, severity_of
from sentinel.simulator import Fault, LogLine, simulate


def test_masking_keeps_structure():
    assert mask("INFO GET /api/v1/users 200 45ms") == "INFO GET /api/v1/users <NUM> <NUM>ms"
    assert "db-1" in mask("ERROR upstream timeout calling db-1 after 1234ms")
    assert "<IP>" in mask("%NTP-6-PEERSYNC: NTP synced to peer 10.0.0.1")


def test_severity():
    assert severity_of("%LINK-3-UPDOWN: Interface Gi0/1, changed state to down") == "critical"
    assert severity_of("%SYS-5-CONFIG_I: Configured from console") == "info"
    assert severity_of("WARN GC pause 900ms heap=92%") == "warning"
    assert severity_of("ERROR boom") == "critical"


def test_miner_merges_variants():
    m = TemplateMiner()
    for state in ("down", "up", "down"):
        m.add(f"%LINK-3-UPDOWN: Interface GigabitEthernet0/{state == 'up' and 3 or 7}, changed state to {state}")
    assert len(m.templates) == 1
    assert m.templates["T001"].text.endswith("changed state to <*>")


def test_new_error_template_raises_alert():
    fault = Fault("F001", "memory_leak", "cache-1", start=400, duration=40, intensity=1.0)
    sim = simulate(seed=2, minutes=720, faults=[fault])
    parsed, miner = parse_logs(sim.logs)
    alerts = detect_log_anomalies(parsed, miner, sim.minutes, sim.warmup)
    oom = [a for a in alerts if a.node == "cache-1" and "OutOfMemory" in a.description]
    assert oom and 400 <= oom[0].start <= 445


def test_routine_logs_do_not_alert():
    lines = [LogLine(t, "web-1", f"INFO GET /api/v1/users 200 {t % 50}ms") for t in range(0, 600, 2)]
    parsed, miner = parse_logs(lines)
    assert detect_log_anomalies(parsed, miner, 600, 180) == []


# ---------------------------------------------------------------- milestone 4: calibrated bursts
def _chatter(rng, minutes, rate=0.04, node="api-1"):
    """Routine warning chatter at `rate` lines per minute (Poisson), like the simulator's retry logs."""
    return [
        LogLine(int(t), node, f"WARN retrying connection to metrics-exporter attempt={1 + t % 2}")
        for t in np.flatnonzero(rng.poisson(rate, minutes))
    ]


def test_poisson_threshold_is_the_smallest_count_under_alpha():
    from math import exp, factorial

    from sentinel.logs import poisson_threshold

    def tail(k, lam):
        return 1 - sum(exp(-lam) * lam**i / factorial(i) for i in range(k))

    for lam, alpha in [(0.2, 1e-3), (0.2, 1e-6), (1.5, 1e-4), (4.0, 1e-5)]:
        k = poisson_threshold(lam, alpha)
        assert tail(k, lam) <= alpha < tail(k - 1, lam)


def test_rare_warning_chatter_does_not_burst_by_chance():
    # 30 days of a routine WARN template at ~0.2 lines per 5 min. The old fixed "3 in 5 min" floor fires
    # 4 times over this span purely by chance; the calibrated test should (almost) never fire.
    lines = _chatter(np.random.default_rng(0), minutes=43200)
    parsed, miner = parse_logs(lines)
    alerts = detect_log_anomalies(parsed, miner, 43200, warmup=1440)
    assert len(alerts) <= 1, [a.description for a in alerts]


def test_real_burst_of_known_template_still_alerts():
    lines = _chatter(np.random.default_rng(1), minutes=900)
    lines += [LogLine(600 + i // 3, "api-1", "WARN retrying connection to metrics-exporter attempt=2") for i in range(15)]
    parsed, miner = parse_logs(sorted(lines, key=lambda ln: ln.t))
    alerts = detect_log_anomalies(parsed, miner, 900, warmup=180)
    assert any(598 <= a.start <= 606 and "burst" in a.description for a in alerts)
