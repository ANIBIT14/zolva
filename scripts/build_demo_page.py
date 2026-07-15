"""Bake the static demo dashboard for the website from the seeded demo DB.

Reads src/zolva/dashboard.html (single source of truth for the UI), injects a
fetch shim that serves the four /api/* routes from baked JSON, and writes
website/demo/index.html. The newest 25 sessions are held back and replayed one
every few seconds so the live tail visibly moves on a static page.

    python scripts/build_demo_page.py   # reseeds the demo DB if missing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from examples.dashboard_demo.seed import DB, seed  # noqa: E402
from zolva.dashboard import session_steps, sessions, stats, topology  # noqa: E402

N_SESSIONS = 200  # newest sessions baked into the page
REPLAY = 25  # of those, held back and replayed as the "live" tail

RIBBON = """
<div class="demo-ribbon">Static demo, seeded data, sessions replay as a live tail.
<a href="/docs/dashboard/">How it works</a> &middot; <a href="/">zolva.ai</a></div>
<style>
  .demo-ribbon { background: #2b1a52; border-bottom: 1px solid #4a3aa7; color: #cfc7e8;
    text-align: center; padding: 0.45rem 1rem; font-size: 12px;
    font-family: ui-monospace, Menlo, monospace; }
  .demo-ribbon a { color: #86b6ef; }
</style>
"""

SHIM = """
<script>
/* Demo shim: serve the four /api routes from baked data; replay the newest
   sessions over time so the live tail moves without a server. */
window.__DEMO = __DEMO_JSON__;
(function () {
  "use strict";
  const D = window.__DEMO;
  const t0 = Date.now();
  // sessions arrive newest-first; hold back the newest REPLAY for the tail
  const pool = D.sessions.sessions.slice(0, D.replay).reverse(); // oldest first
  const base = D.sessions.sessions.slice(D.replay);

  function visible() {
    const released = Math.min(pool.length, Math.floor((Date.now() - t0) / 4000));
    return base.concat(pool.slice(0, released));
  }
  function payload(url) {
    const u = new URL(url, location.href);
    if (u.pathname === "/api/topology") return D.topology;
    if (u.pathname === "/api/stats") return D.stats;
    const m = u.pathname.match(/^\\/api\\/sessions\\/(.+)\\/steps$/);
    if (m) {
      const sid = decodeURIComponent(m[1]);
      return D.steps[sid] || { session_id: sid, steps: [] };
    }
    if (u.pathname === "/api/sessions") {
      const after = Number(u.searchParams.get("after_id") || 0);
      const vis = visible();
      const cursor = vis.reduce((a, s) => Math.max(a, s.last_id), 0);
      return { cursor: cursor, sessions: vis.filter((s) => s.last_id > after) };
    }
    return null;
  }
  window.fetch = async function (url) {
    const body = payload(url);
    if (body === null) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(body), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };
})();
</script>
"""


def main() -> None:
    if not DB.is_file():
        print("seeding demo DB...")
        seed()
    db = str(DB)

    sess = sessions(db, limit=N_SESSIONS)
    data = {
        "replay": REPLAY,
        "topology": topology(str(ROOT / "examples/dashboard_demo/agents")),
        "stats": stats(db),
        "sessions": sess,
        "steps": {
            s["session_id"]: session_steps(db, s["session_id"]) for s in sess["sessions"]
        },
    }

    html = (ROOT / "src/zolva/dashboard.html").read_text()
    html = html.replace("<title>Zolva Dashboard</title>",
                        "<title>Zolva Dashboard Demo</title>", 1)
    html = html.replace("</header>", "</header>" + RIBBON, 1)
    shim = SHIM.replace("__DEMO_JSON__", json.dumps(data, separators=(",", ":")))
    html = html.replace("<script>\n\"use strict\";", shim + "<script>\n\"use strict\";", 1)
    assert shim in html, "shim injection point not found in dashboard.html"

    out = ROOT / "website/demo/index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB, "
          f"{len(sess['sessions'])} sessions, {REPLAY} replayed live)")


if __name__ == "__main__":
    main()
