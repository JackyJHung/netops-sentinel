"""Log template mining (a compact take on Drain) and log-based anomaly alerts.

Raw lines like
    %LINK-3-UPDOWN: Interface GigabitEthernet0/7, changed state to down
become templates like
    %LINK-3-UPDOWN: Interface GigabitEthernet<*>, changed state to <*>
so thousands of lines collapse into a few dozen event types whose *rates*
can be monitored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from .detection import Alert, intervals
from .simulator import LogLine

_MASKS = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),
    (re.compile(r"\bid=[0-9a-f]{6,}\b"), "id=<HEX>"),
    (re.compile(r"(?<=[A-Za-z])\d+/\d+"), "<IF>"),
    (re.compile(r"(?<![A-Za-z]-)\b\d+(?:\.\d+)?(?=ms\b|%|\b)"), "<NUM>"),
]
_SYSLOG_LEVEL = re.compile(r"^%[A-Z0-9_]+-(\d)-[A-Z0-9_]+:")


def mask(message: str) -> str:
    out = message
    for pattern, token in _MASKS:
        out = pattern.sub(token, out)
    return out


def severity_of(message: str) -> str:
    """Map syslog levels (0-7) and app log levels to critical/warning/info."""
    m = _SYSLOG_LEVEL.match(message)
    if m:
        level = int(m.group(1))
        return "critical" if level <= 3 else "warning" if level == 4 else "info"
    head = message.split(" ", 1)[0].upper()
    if head in ("ERROR", "CRITICAL", "FATAL"):
        return "critical"
    if head in ("WARN", "WARNING"):
        return "warning"
    return "info"


@dataclass
class Template:
    template_id: str
    tokens: list[str]
    count: int = 0
    severity: str = "info"

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


@dataclass
class TemplateMiner:
    """Groups lines by (token count, first token), then merges lines whose
    tokens match a template at >= `similarity` of positions."""

    similarity: float = 0.6
    templates: dict[str, Template] = field(default_factory=dict)
    _groups: dict[tuple[int, str], list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, message: str) -> Template:
        tokens = mask(message).split()
        key = (len(tokens), tokens[0] if tokens else "")
        best, best_sim = None, -1.0
        for tid in self._groups[key]:
            tpl = self.templates[tid]
            same = sum(a == b for a, b in zip(tpl.tokens, tokens) if a != "<*>")
            sim = same / max(len(tokens), 1)
            if sim > best_sim:
                best, best_sim = tpl, sim
        if best is not None and best_sim >= self.similarity:
            best.tokens = [a if a == b else "<*>" for a, b in zip(best.tokens, tokens)]
            best.count += 1
            return best
        tid = f"T{len(self.templates) + 1:03d}"
        tpl = Template(tid, tokens, 1, severity_of(message))
        self.templates[tid] = tpl
        self._groups[key].append(tid)
        return tpl


@dataclass
class ParsedLog:
    t: int
    node: str
    template_id: str
    severity: str


def parse_logs(lines: list[LogLine], miner: TemplateMiner | None = None) -> tuple[list[ParsedLog], TemplateMiner]:
    miner = miner or TemplateMiner()
    parsed = [ParsedLog(ln.t, ln.node, miner.add(ln.message).template_id, severity_of(ln.message)) for ln in lines]
    return parsed, miner


def detect_log_anomalies(
    parsed: list[ParsedLog],
    miner: TemplateMiner,
    minutes: int,
    warmup: int = 180,
    window: int = 5,
    burst_factor: float = 5.0,
    min_count: int = 3,
    start_id: int = 0,
) -> list[Alert]:
    """Flag (node, template) pairs that are new since warm-up and warning+,
    or whose rolling count bursts well above their warm-up rate."""
    counts: dict[tuple[str, str], np.ndarray] = defaultdict(lambda: np.zeros(minutes))
    for p in parsed:
        counts[(p.node, p.template_id)][p.t] += 1

    alerts: list[Alert] = []
    kernel = np.ones(window)
    for (node, tid), series in counts.items():
        tpl = miner.templates[tid]
        base_rate = series[:warmup].sum() / max(warmup, 1) * window  # expected per window
        rolling = np.convolve(series, kernel)[:minutes]  # trailing window count
        if base_rate == 0:
            if tpl.severity == "info":
                continue
            mask = rolling >= min(min_count, 2)
            reason = "new event type"
        elif tpl.severity == "info":
            # routine chatter: only a large surge is interesting
            mask = rolling >= max(4 * min_count, 3 * burst_factor * base_rate)
            reason = f"surge vs baseline {base_rate:.2f}/{window}min"
        else:
            mask = rolling >= max(min_count, burst_factor * base_rate)
            reason = f"burst vs baseline {base_rate:.2f}/{window}min"
        mask[:warmup] = False
        for a, b in intervals(mask, min_len=2, max_gap=window):
            peak = float(rolling[a:b].max())
            alerts.append(
                Alert(
                    alert_id=f"L{start_id + len(alerts) + 1:05d}",
                    node=node,
                    signal=f"log:{tid}",
                    detector="log_template",
                    start=a,
                    end=b,
                    peak_score=peak,
                    severity="critical" if tpl.severity == "critical" else "warning",
                    description=f"{reason}: '{tpl.text}' x{int(peak)} on {node}",
                )
            )
    return alerts
