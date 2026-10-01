"""Change-aware paging: decide when (and whether) an incident pages someone.

Planned changes (config pushes, rolling restarts) cause short, real anomalies.
Suppressing every alert after a change would be wrong, because changes also
cause real faults. Instead, when a recorded change hit one of an incident's
nodes shortly before the incident started, the page is *held* for a grace
period:

  * if every alert has cleared by the end of the hold, the incident is
    suppressed (logged as change-related, nobody is woken up);
  * otherwise it pages when the hold expires, and the summary names the change.

Only information available at the end of the hold is used, so this works the
same way a live pager would. The cost is detection delay on faults that follow
a change, which the evaluation measures (MTTD counts from the page).
"""

from __future__ import annotations

from .correlation import ORIGIN_TOL, Incident


def apply_change_hold(incidents: list[Incident], changes: list, window: int = 15, grace: int = 10) -> None:
    """Set `held`, `suppressed`, `paged_at` (and `hold_change`) on each incident. `grace <= 0` disables holding."""
    for inc in incidents:
        start = inc.start
        nodes = set(inc.nodes) | {s.node for s in inc.silences}
        recent = [c for c in changes if c.node in nodes and start - window <= c.t <= start + ORIGIN_TOL]
        inc.held = grace > 0 and bool(recent)
        inc.hold_change = max(recent, key=lambda c: c.t) if inc.held else None
        if not inc.held:
            inc.suppressed, inc.paged_at = False, start
        elif inc.end <= start + grace:
            inc.suppressed, inc.paged_at = True, None
        else:
            inc.suppressed, inc.paged_at = False, start + grace
