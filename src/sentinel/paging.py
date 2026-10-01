"""Change-aware paging: decide when (and whether) an incident pages someone.

Planned changes (config pushes, rolling restarts) cause short, real anomalies.
Suppressing every alert after a change would be wrong, because changes also
cause real faults. Instead, when a recorded change hit one of an incident's
nodes shortly before the incident started, the page is *held* for a grace
period:

  * if every alert has cleared by the end of the hold, the incident is
    suppressed (logged as change-related, nobody is woken up);
  * otherwise it pages when the hold expires, and the summary names the change.

With `per_root=True`, an incident with several declared root causes is split
into one alert group per root (each alert goes to the root upstream of it with
the most recent onset), and each group is held or not on its own. The incident
pages as soon as any group needs to, and is suppressed only if every group is.
A concurrent fault therefore does not wait for its partner's change.

Only information available at the end of the hold is used, so this works the
same way a live pager would. The cost is detection delay on faults that follow
a change, which the evaluation measures (MTTD counts from the page).
"""

from __future__ import annotations

from .correlation import ORIGIN_TOL, Incident
from .detection import Alert
from .topology import Topology


def _root_groups(inc: Incident, topo: Topology) -> list[tuple[str | None, list[Alert]]]:
    roots = inc.roots
    if len(roots) <= 1:
        return [(roots[0] if roots else None, inc.alerts)]
    onset = {r: min((a.start for a in inc.alerts if a.node == r), default=None) for r in roots}
    for s in inc.silences:
        if s.node in onset:
            onset[s.node] = s.start if onset[s.node] is None else min(onset[s.node], s.start)
    onset = {r: (t if t is not None else inc.start) for r, t in onset.items()}
    groups: dict[str, list[Alert]] = {r: [] for r in roots}
    for a in inc.alerts:
        cands = [r for r in roots if r == a.node or r in topo.upstream(a.node)] or list(roots)
        before = [r for r in cands if onset[r] <= a.start + ORIGIN_TOL]
        home = max(before, key=lambda r: onset[r]) if before else min(cands, key=lambda r: onset[r])
        groups[home].append(a)
    return [(r, g) for r, g in groups.items() if g]


def _decide(nodes: set[str], start: int, end: int, changes: list, window: int, grace: int):
    """(held, suppressed, paged_at, change) for one group of alerts."""
    recent = [c for c in changes if c.node in nodes and start - window <= c.t <= start + ORIGIN_TOL]
    if grace <= 0 or not recent:
        return False, False, start, None
    change = max(recent, key=lambda c: c.t)
    if end <= start + grace:
        return True, True, None, change
    return True, False, start + grace, change


def apply_change_hold(
    incidents: list[Incident],
    changes: list,
    window: int = 15,
    grace: int = 10,
    per_root: bool = False,
    topo: Topology | None = None,
) -> None:
    """Set `held`, `suppressed`, `paged_at` (and `hold_change`) on each incident. `grace <= 0` disables holding."""
    topo = topo or Topology.default()
    for inc in incidents:
        silent = {s.node for s in inc.silences}
        groups = _root_groups(inc, topo) if per_root else [(None, inc.alerts)]
        if len(groups) == 1:
            decisions = [_decide(set(inc.nodes) | silent, inc.start, inc.end, changes, window, grace)]
        else:
            decisions = [
                _decide({a.node for a in g} | ({root} & silent) | {root}, min(a.start for a in g), max(a.end for a in g),
                        changes, window, grace)
                for root, g in groups
            ]
        pages = [d[2] for d in decisions if not d[1]]
        held_changes = [d[3] for d in decisions if d[0]]
        inc.held = bool(held_changes)
        inc.hold_change = max(held_changes, key=lambda c: c.t) if held_changes else None
        inc.suppressed = not pages
        inc.paged_at = min(pages) if pages else None
