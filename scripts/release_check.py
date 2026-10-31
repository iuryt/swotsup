"""Before production: the app's main paths timed in a headless browser against a server (the dev EC2 by default), each
against a limit; fails (exit 1) if one is slower, a request fails, or the page logs an error. The page is opened with
?debug=1, so the server's debug log has each tile's split (queue, server, network, decode, color) for a closer look.

    python scripts/release_check.py [URL] [REPORT.json]     (sbatch slurm/run.sbatch scripts/release_check.py URL)
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

URL = (sys.argv[1] if len(sys.argv) > 1 else "https://54-85-79-178.sslip.io/").rstrip("/") + "/"
REPORT = sys.argv[2] if len(sys.argv) > 2 else None
LIMITS = {  # seconds
    "open": 10, "new window": 3, "globe to the Pacific": 3, "2 km, a new place": 5, "250 m, a new place": 8,
    "250 m, again": 1, "click": 3, "time series": 15, "profile": 5, "hovmoller": 30, "link reopened": 10}
results, problems = [], []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text[:200]}"))
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    page.on("response", lambda r: "/api/" in r.url and r.status >= 400 and problems.append(f"{r.status} {r.url[:160]}"))

    page.add_init_script("""window.OURS = () => ['under', 'swot', 'detail'].every((id) => !map.getSource(id)
      || map.isSourceLoaded(id)); window.BASE = () => map.isSourceLoaded('basemap');""")

    def settled(timeout):
        """The view read and every tile in view drawn."""
        page.wait_for_function("typeof map !== 'undefined' && typeof state !== 'undefined' && state.data"
                               " && document.querySelector('#busy').classList.contains('hidden')"
                               " && OURS() && !map.isMoving()", timeout=timeout, polling=100)

    def timed(name, act, done):
        limit = LIMITS[name]
        t0 = time.perf_counter()
        act()
        try:
            done(max(4 * limit, 60) * 1000)
            seconds = time.perf_counter() - t0
        except Exception as e:  # past 4x its limit (60 s at least): recorded as failed
            seconds, problems_ = None, f"{name}: {str(e).splitlines()[0]}"
            problems.append(problems_)
        try:  # the basemap (not ours): how much longer it took, for the record
            page.wait_for_function("BASE()", timeout=60_000, polling=100)
            base = round(time.perf_counter() - t0, 2)
        except Exception:
            base = None
        ok = seconds is not None and seconds <= limit
        results.append(dict(step=name, seconds=None if seconds is None else round(seconds, 2), limit=limit, ok=ok,
                            basemap=base))
        print(f"{'ok  ' if ok else 'SLOW' if seconds else 'FAIL'} {name:24s} {seconds if seconds is None else round(seconds, 2)} s"
              f" (limit {limit} s; basemap done at {base} s)", flush=True)

    timed("open", lambda: page.goto(URL + "?debug=1", wait_until="commit", timeout=120_000), settled)
    timed("new window", lambda: page.evaluate("setDates(state.start - 60 * DAY, 21)"), settled)
    timed("globe to the Pacific", lambda: page.evaluate("map.jumpTo({center: [-150, 0], zoom: 2.5})"), settled)
    timed("2 km, a new place", lambda: page.evaluate("map.jumpTo({center: [-40, -32], zoom: 6.5})"), settled)
    timed("250 m, a new place", lambda: page.evaluate("map.jumpTo({center: [-58, -44], zoom: 9})"), settled)
    page.evaluate("map.jumpTo({center: [-58, -44], zoom: 6.5})")
    settled(60_000)
    timed("250 m, again", lambda: page.evaluate("map.jumpTo({center: [-58, -44], zoom: 9})"), settled)
    timed("click", lambda: page.mouse.click(800, 475),
          lambda t: page.wait_for_selector(".card .hint:not(:has(.spinner))", timeout=t))
    timed("time series", lambda: page.click(".card .expand"), lambda t: page.wait_for_function(
        "document.querySelector('.card .uplot') && !document.querySelector('.card .hint').textContent.includes('reading')",
        timeout=t, polling=100))
    page.evaluate("map.jumpTo({center: [-58, -44], zoom: 6.5})")
    settled(60_000)

    def draw():
        page.click("#line")
        for xy in ((600, 400), (1000, 520)):
            page.mouse.click(*xy)
            page.wait_for_timeout(200)
        page.mouse.dblclick(1000, 520)

    timed("profile", draw, lambda t: page.wait_for_selector(".card.transect .uplot", timeout=t))
    timed("hovmoller", lambda: page.click(".card.transect .kind [data-v=hovmoller]"), lambda t: page.wait_for_selector(
        ".card.transect .uplot, .card.transect .loading:not(:has(.spinner))", timeout=t))
    page.evaluate("linkNow()")
    link = page.evaluate("location.href").replace("?debug=1", "")
    other = browser.new_page(viewport={"width": 1600, "height": 950})
    other.on("pageerror", lambda e: problems.append(f"page error (link): {e}"))
    t0 = time.perf_counter()

    def reopened(t):
        other.wait_for_function("typeof state !== 'undefined' && state.data", timeout=t, polling=100)
        other.wait_for_selector(".card.transect .uplot", timeout=t)
        other.wait_for_function("['under', 'swot', 'detail'].every((id) => !map.getSource(id) || map.isSourceLoaded(id))",
                                timeout=t, polling=100)

    timed("link reopened", lambda: other.goto(link, wait_until="commit", timeout=120_000), reopened)
    same = other.evaluate("[location.hash, map.getZoom().toFixed(1)]")
    if same[1] != page.evaluate("map.getZoom().toFixed(1)"):
        problems.append(f"link reopened at zoom {same[1]}, not the page's")
    browser.close()

failed = [r for r in results if not r["ok"]]
print(f"\n{len(results) - len(failed)} of {len(results)} within their limits; {len(problems)} problems", flush=True)
for x in problems:
    print("  " + x, flush=True)
if REPORT:
    with open(REPORT, "w") as f:
        json.dump(dict(url=URL, time=time.strftime("%Y-%m-%dT%H:%M:%S"), results=results, problems=problems), f, indent=1)
sys.exit(1 if failed or problems else 0)
