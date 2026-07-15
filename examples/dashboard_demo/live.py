"""Append a fresh session every few seconds so the dashboard's live tail moves.

Run seed.py first, open the dashboard, then run this in a second terminal:

    python examples/dashboard_demo/live.py
"""

from __future__ import annotations

import random
import time

from zolva.audit import AuditLog
from zolva.bus import Step

from examples.dashboard_demo.seed import _SCENARIOS, DB


def main() -> None:
    rng = random.Random()
    log = AuditLog(DB)
    scenarios = [(agent, fn) for agent, weight, fn in _SCENARIOS for _ in range(weight)]
    print(f"appending live sessions to {DB} (ctrl-c to stop)")
    while True:
        agent, fn = rng.choice(scenarios)
        sid = f"s-live-{rng.randrange(16**6):06x}"
        for step_type, data in fn(rng):  # type: ignore[operator]
            log.append(Step(type=step_type, session_id=sid, agent=agent, data=data))  # type: ignore[arg-type]
            time.sleep(0.4)
        print(f"  {agent}  {sid}")
        time.sleep(rng.uniform(1, 4))


if __name__ == "__main__":
    main()
