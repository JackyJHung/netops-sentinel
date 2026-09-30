"""Metric anomaly detectors and alert generation.

Every detector maps a 1-D series to a per-minute anomaly score; points above
the detector's threshold are anomalous. Runs of anomalous points are
debounced into `Alert`s so a single noisy minute doesn't page anyone.

Detectors:
  * StaticThreshold   - the "before AIOps" baseline: fixed limits per metric
  * RobustZScore      - rolling median/MAD, resistant to outliers
  * EWMAControl       - exponentially weighted mean/variance control chart
                        with baseline freezing while anomalous
  * SaturationForecast - trend projection: "memory will hit 95% in ~40 min"
  * NodeIsolationForest - multivariate per-node model (scikit-learn)

Missing telemetry arrives as NaN. Every detector treats a missing point as
"no evidence": it never scores as anomalous, it is not learned into any
baseline, and windows with too few real points produce no score at all.
Series without NaN take the original code paths, so results are unchanged.
"""

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

# Per-metric floors for the noise scale, so near-constant series (e.g. packet
# loss ~0) don't turn tiny wiggles into huge z-scores.
SCALE_FLOOR = {  # (absolute floor, relative-to-median floor)
    "cpu_pct": (2.0, 0.0),
    "mem_pct": (1.0, 0.0),
    "latency_ms": (0.0, 0.10),
    "packet_loss_pct": (0.5, 0.0),
    "error_rate_pct": (0.5, 0.0),
}

def _quiet_nanmedian(a: np.ndarray, axis=None) -> np.ndarray:
    """nanmedian without the 'All-NaN slice' warning (an all-missing window is expected, not an error)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(a, axis=axis)


STATIC_LIMITS = {
    "cpu_pct": 85.0,
    "mem_pct": 90.0,
    "packet_loss_pct": 2.0,
    "error_rate_pct": 5.0,
    "latency_ms": None,  # set per node as 4x the warm-up median (a typical hand-tuned rule)
}


@dataclass
class Alert:
    alert_id: str
    node: str
    signal: str  # metric name, "multivariate", or "log:<template>"
    detector: str
    start: int
    end: int
    peak_score: float
    severity: str  # warning | critical
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Univariate detectors
# --------------------------------------------------------------------------
class StaticThreshold:
    name = "static"

    def __init__(self, metric: str, warmup: int = 180):
        self.metric = metric
        self.warmup = warmup
        self.threshold = 1.0

    def score(self, x: np.ndarray) -> np.ndarray:
        limit = STATIC_LIMITS[self.metric]
        if limit is None:
            limit = 4.0 * float(_quiet_nanmedian(x[: self.warmup]))
        return x / max(limit, 1e-9)  # NaN in, NaN out: never above threshold


class RobustZScore:
    """Upward deviation from a rolling median, scaled by MAD."""

    name = "robust_z"

    def __init__(self, metric: str, window: int = 120, threshold: float = 5.0):
        self.metric = metric
        self.window = window
        self.threshold = threshold
        self.min_valid = max(5, window // 4)  # fewer real points than this in the history: no score

    def score(self, x: np.ndarray) -> np.ndarray:
        n, w = len(x), self.window
        med = np.full(n, np.nan)
        mad = np.full(n, np.nan)
        if n > w:
            # windows[i] = x[i : i + w]  ->  history for point i + w
            windows = np.lib.stride_tricks.sliding_window_view(x[:-1], w)
            if np.isnan(x).any():
                m = _quiet_nanmedian(windows, axis=1)
                d = _quiet_nanmedian(np.abs(windows - m[:, None]), axis=1)
                thin = (~np.isnan(windows)).sum(axis=1) < self.min_valid
                m[thin], d[thin] = np.nan, np.nan
            else:
                m = np.median(windows, axis=1)
                d = np.median(np.abs(windows - m[:, None]), axis=1)
            med[w:] = m
            mad[w:] = d
        abs_floor, rel_floor = SCALE_FLOOR.get(self.metric, (1e-6, 0.0))
        scale = np.maximum.reduce([1.4826 * mad, rel_floor * np.abs(med), np.full(n, abs_floor)])
        z = (x - med) / np.maximum(scale, 1e-9)
        return np.nan_to_num(z, nan=0.0)


class EWMAControl:
    """EWMA control chart. The baseline stops updating while a point is anomalous,
    so a long incident doesn't get absorbed into 'normal'."""

    name = "ewma"

    def __init__(self, metric: str, alpha: float = 0.05, threshold: float = 6.0, warmup: int = 60):
        self.metric = metric
        self.alpha = alpha
        self.threshold = threshold
        self.warmup = warmup

    def score(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        out = np.zeros(n)
        w = min(self.warmup, n)
        head = x[:w][~np.isnan(x[:w])]
        mean = float(np.mean(head)) if head.size else float("nan")
        var = float(np.var(head)) if head.size else 0.0
        abs_floor, rel_floor = SCALE_FLOOR.get(self.metric, (1e-6, 0.0))
        for i in range(w, n):
            if x[i] != x[i]:  # missing point: no score, no baseline update
                continue
            if mean != mean:  # nothing seen yet: start the baseline here
                mean = float(x[i])
                continue
            sd = max(np.sqrt(var), abs_floor, rel_floor * abs(mean), 1e-9)
            z = (x[i] - mean) / sd
            out[i] = z
            if z < self.threshold:  # only learn from normal-looking points
                diff = x[i] - mean
                mean += self.alpha * diff
                var = (1 - self.alpha) * (var + self.alpha * diff * diff)
        return out


class SaturationForecast:
    """Predictive alerting for resources that fill up (memory, disk).

    Fits a least-squares trend over a short trailing window and projects it
    forward. Fires when the projection crosses `limit` within `horizon`
    minutes AND the slope is statistically solid (t-stat), which catches slow
    leaks long before a static 90% threshold would.
    """

    name = "forecast"

    def __init__(self, metric: str = "mem_pct", window: int = 20, horizon: int = 120, limit: float = 95.0, min_t: float = 4.0):
        self.metric = metric
        self.window = window
        self.horizon = horizon
        self.limit = limit
        self.min_t = min_t
        self.threshold = 1.0

    def fit(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (slope per minute, minutes until `limit`) for each point."""
        n, w = len(x), self.window
        slope = np.zeros(n)
        eta = np.full(n, np.inf)
        if n < w:
            return slope, eta
        if np.isnan(x).any():
            return self._fit_with_gaps(x, slope, eta)
        win = np.lib.stride_tricks.sliding_window_view(x, w)  # win[i] ends at point i + w - 1
        t = np.arange(w) - (w - 1) / 2
        b = (win * t).sum(axis=1) / (t * t).sum()
        resid = win - win.mean(axis=1, keepdims=True) - b[:, None] * t
        se = np.sqrt((resid**2).sum(axis=1) / (w - 2) / (t * t).sum())
        tstat = b / np.maximum(se, 1e-9)
        last = win[:, -1]
        with np.errstate(divide="ignore", invalid="ignore"):
            steps = np.where((b > 0) & (tstat >= self.min_t), (self.limit - last) / b, np.inf)
        slope[w - 1 :] = b
        eta[w - 1 :] = np.maximum(steps, 0)
        return slope, eta

    def _fit_with_gaps(self, x: np.ndarray, slope: np.ndarray, eta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Least squares over the real points in each window; windows under 60% real points give no forecast."""
        w = self.window
        valid = ~np.isnan(x)
        V = np.lib.stride_tricks.sliding_window_view(valid.astype(float), w)
        X = np.lib.stride_tricks.sliding_window_view(np.where(valid, x, 0.0), w)
        t = np.arange(w, dtype=float)
        cnt, st, stt = V.sum(axis=1), V @ t, V @ (t * t)
        sx, stx = X.sum(axis=1), X @ t
        with np.errstate(divide="ignore", invalid="ignore"):
            sxx = stt - st * st / cnt
            b = (stx - st * sx / cnt) / sxx
            a = (sx - b * st) / cnt
            resid = (X - a[:, None] - b[:, None] * t) * V
            se = np.sqrt((resid**2).sum(axis=1) / (cnt - 2) / sxx)
            tstat = b / np.maximum(se, 1e-9)
            last = a + b * (w - 1)  # fitted value at the window end (the point itself may be missing)
            ok = (cnt >= 0.6 * w) & (b > 0) & (tstat >= self.min_t)
            steps = np.where(ok, (self.limit - last) / b, np.inf)
        slope[w - 1 :] = np.where(ok, b, 0.0)
        eta[w - 1 :] = np.maximum(steps, 0)
        return slope, eta

    def score(self, x: np.ndarray) -> np.ndarray:
        _, eta = self.fit(x)
        # 1.0 when the limit is exactly `horizon` away, larger as it gets closer
        return np.where(np.isfinite(eta), self.horizon / np.maximum(eta, 1.0), 0.0)


# --------------------------------------------------------------------------
# Multivariate detector
# --------------------------------------------------------------------------
class NodeIsolationForest:
    """Isolation Forest over a node's *residuals* (each metric's robust z-score
    against a short rolling baseline). Using residuals instead of raw values keeps
    the daily cycle from looking anomalous, while still catching combinations
    of metrics that are each only mildly off."""

    name = "iforest"

    def __init__(self, warmup: int = 180, window: int = 30, seed: int = 0, margin: float = 0.05):
        self.warmup = warmup
        self.window = window
        self.seed = seed
        self.margin = margin
        self.threshold = 0.0

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        cols = list(frame.columns)
        Z = np.column_stack(
            [RobustZScore(m, window=self.window).score(frame[m].to_numpy(dtype=float)) for m in cols]
        )
        Z = np.clip(Z, -20, 20)
        train = Z[self.window : self.warmup]
        model = IsolationForest(n_estimators=200, random_state=self.seed).fit(train)
        s = -model.score_samples(Z)  # higher = more anomalous
        cutoff = np.max(s[self.window : self.warmup]) + self.margin
        out = s - cutoff  # > 0 means anomalous
        out[frame.isna().to_numpy().all(axis=1)] = -1.0  # a node that sent nothing is not scored here
        return out


# --------------------------------------------------------------------------
# Scores -> alerts
# --------------------------------------------------------------------------
def intervals(mask: np.ndarray, min_len: int = 3, max_gap: int = 2) -> list[tuple[int, int]]:
    """Turn a boolean mask into [start, end) runs, bridging short gaps and dropping blips."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs: list[list[int]] = [[int(idx[0]), int(idx[0]) + 1]]
    for i in idx[1:]:
        if i - runs[-1][1] <= max_gap:
            runs[-1][1] = int(i) + 1
        else:
            runs.append([int(i), int(i) + 1])
    return [(a, b) for a, b in runs if int(mask[a:b].sum()) >= min_len]


def _alerts_from_score(node, signal, detector, score, threshold, start_id, min_len=3):
    alerts = []
    for a, b in intervals(score > threshold, min_len=min_len):
        peak = float(np.nanmax(score[a:b]))
        sev = "critical" if peak > max(2 * threshold, threshold + 0.1) else "warning"
        alerts.append(
            Alert(
                alert_id=f"A{start_id + len(alerts):05d}",
                node=node,
                signal=signal,
                detector=detector,
                start=a,
                end=b,
                peak_score=round(peak, 2),
                severity=sev,
                description=f"{signal} anomalous on {node} ({detector}, peak score {peak:.1f})",
            )
        )
    return alerts


def detect_metric_anomalies(
    metrics: pd.DataFrame,
    detectors: tuple[str, ...] = ("robust_z", "ewma", "iforest", "forecast"),
    warmup: int = 180,
    min_len: int = 3,
    cache: dict | None = None,
) -> list[Alert]:
    """Run the configured detectors over every (node, metric) series.

    Univariate detectors vote: a point is anomalous when *both* robust_z and
    ewma agree (if both are enabled), which cuts false positives from either
    one alone. The Isolation Forest emits its own node-level alerts.

    `cache` (optional, one dict per simulated day) memoizes the Isolation
    Forest scores, so benchmark configs that share the detector don't refit it.
    """
    alerts: list[Alert] = []
    nodes = list(dict.fromkeys(metrics.columns.get_level_values(0)))

    for node in nodes:
        for metric in metrics[node].columns:
            x = metrics[(node, metric)].to_numpy(dtype=float)
            if "static" in detectors:
                d = StaticThreshold(metric, warmup)
                alerts += _alerts_from_score(node, metric, d.name, d.score(x), d.threshold, len(alerts), min_len)
            if "forecast" in detectors and metric == "mem_pct":
                d = SaturationForecast(metric)
                sc = d.score(x)
                sc[:warmup] = 0.0
                for a in _alerts_from_score(node, metric, d.name, sc, d.threshold, len(alerts), min_len):
                    eta = d.horizon / max(a.peak_score, 1e-9)
                    a.description = f"{metric} on {node} trending to {d.limit:.0f}% in ~{eta:.0f} min (forecast)"
                    alerts.append(a)
            uni = []
            if "robust_z" in detectors:
                d = RobustZScore(metric)
                uni.append((d.name, d.score(x) / d.threshold))
            if "ewma" in detectors:
                d = EWMAControl(metric)
                uni.append((d.name, d.score(x) / d.threshold))
            if uni:
                name = "+".join(n for n, _ in uni)
                combined = np.minimum.reduce([s for _, s in uni])  # all must agree
                combined[:warmup] = 0.0
                alerts += _alerts_from_score(node, metric, name, combined, 1.0, len(alerts), min_len)

        if "iforest" in detectors:
            d = NodeIsolationForest(warmup)
            key = ("iforest", node, warmup)
            s = cache.get(key) if cache is not None else None
            if s is None:
                s = d.score(metrics[node])
                s[:warmup] = -1.0
                if cache is not None:
                    cache[key] = s
            alerts += _alerts_from_score(node, "multivariate", d.name, s, 0.0, len(alerts), min_len)

    for i, a in enumerate(alerts):
        a.alert_id = f"A{i + 1:05d}"
    return alerts
