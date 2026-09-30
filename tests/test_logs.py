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
