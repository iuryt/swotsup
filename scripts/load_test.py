"""How the server holds up with many visitors at once.

    load_test.py record URL OUT.json [PLACES]   real browser sessions (headless chromium): every /api/ request each
                                                 story makes, with its start time in the story
    load_test.py replay URL STORIES.json LEVELS [MINUTES]   replay them as many visitors at once (LEVELS like 5,20,50)

A story is what one action asks for: the opening page; a region at zoom 6 with a point's series; the 250 m at zoom
9.5; a line's profile; a line's Hovmoller. A visitor opens the page, then goes from place to place: a region, maybe
the 250 m, a profile, a Hovmoller, with a pause to look at each. Each visitor sends its requests as the browser did
(at their times in the story, at most 6 at once) and has its own X-Forwarded-For, as different people would.
Prints, per level, the time of each kind of request (median, 95th percentile, max), errors, and how long each story
took to complete (what someone waits for). Record against a server whose memory is then cleared (a restart), so the
replay starts cold."""
import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from urllib.parse import urlsplit

PLACES = [(-60, 38), (-30, 40), (-40, 10), (-20, -20), (0, -40), (30, -38), (60, -10), (80, -30), (90, 10), (120, 20),
          (150, 30), (170, 0), (-170, 20), (-150, -20), (-130, 30), (-120, -40), (-100, -5), (-85, -3), (-55, 7),
          (-10, 50), (5, 70), (140, -50), (110, -45), (-70, -55), (50, 15)]
VIEW = "view=-180,180,-90,90,{start},21,ssha_dtu25_cal"


def kind(path):
    for k in ("basemap", "detail", "tile", "series", "transect", "hovmoller", "value", "limits", "load", "timeline"):
        if f"/api/{k}" in path:
            return k
    return "other"


def stories(start, places):
    """Every story's address after the #: (name, hash)."""
    v = VIEW.format(start=start)
    out = [("open", "")]
    for lon, lat in places:
        out += [(f"region {lon},{lat}", f"{v}&map={lon},{lat},6,globe&point={lon},{lat},series"),
                (f"detail {lon},{lat}", f"{v}&map={lon},{lat},9.5,globe"),
                (f"profile {lon},{lat}", f"{v}&map={lon},{lat},6,globe&line={lon - 1.5},{lat};{lon + 1.5},{lat}/profile/2/time"),
                (f"hovmoller {lon},{lat}", f"{v}&map={lon},{lat},6,globe&line={lon - 1.5},{lat};{lon + 1.5},{lat}/hovmoller/2/time")]
    return out


def record(url, out, n):
    from playwright.sync_api import sync_playwright
    import urllib.request
    info = json.load(urllib.request.urlopen(url + "api/info"))
    last = info["last"][:10]
    start = time.strftime("%Y-%m-%d", time.gmtime(time.mktime(time.strptime(last, "%Y-%m-%d")) - 20 * 86400))
    result = []
    with sync_playwright() as p:
        for name, h in stories(start, PLACES[:n]):
            # a browser per story (a crash costs one story), with software WebGL as in screenshot.py
            browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            t0, reqs = time.monotonic(), []
            page.on("request", lambda r: "/api/" in r.url and "/api/debug" not in r.url and reqs.append(
                (round(time.monotonic() - t0, 3), r.method, urlsplit(r.url).path + "?" + urlsplit(r.url).query,
                 r.post_data)))
            try:
                page.goto(url + ("#" + h if h else ""))
                page.wait_for_load_state("networkidle", timeout=180_000)
                page.wait_for_timeout(3000)  # a card's second request (the 250 m series) after the first
                page.wait_for_load_state("networkidle", timeout=180_000)
            except Exception as e:
                print(name, "not idle:", e, flush=True)
            print(f"{name}: {len(reqs)} requests, {time.monotonic() - t0:.0f} s", flush=True)
            result.append({"name": name, "requests": reqs})
            browser.close()
            json.dump({"url": url, "start": start, "stories": result}, open(out, "w"))  # after each story


async def replay_level(url, recorded, users, minutes):
    import httpx
    by = {s["name"]: s["requests"] for s in recorded["stories"]}
    places = sorted({n.split(" ", 1)[1] for n in by if " " in n})
    times, story_times, errors = defaultdict(list), defaultdict(list), defaultdict(int)
    stop = time.monotonic() + minutes * 60

    async def one(client, method, path, body, sem):
        async with sem:
            t = time.monotonic()
            try:
                r = await client.request(method, url.rstrip("/") + path, content=body, timeout=180)
                await r.aread()
                ok = r.status_code < 400
                if not ok:
                    errors[f"{kind(path)} {r.status_code}"] += 1
            except Exception as e:
                ok = False
                errors[f"{kind(path)} {type(e).__name__}"] += 1
            if ok:
                times[kind(path)].append(time.monotonic() - t)

    async def story(client, name):
        sem, t0, tasks = asyncio.Semaphore(6), time.monotonic(), []
        for at, method, path, body in by[name]:
            await asyncio.sleep(max(0, t0 + at - time.monotonic()))
            tasks.append(asyncio.create_task(one(client, method, path, body, sem)))
        await asyncio.gather(*tasks)
        story_times[name.split(" ")[0]].append(time.monotonic() - t0)

    async def visitor(k):
        await asyncio.sleep(random.uniform(0, 30))  # arrivals spread over half a minute
        headers = {"X-Forwarded-For": f"10.{k // 250}.{k % 250}.{random.randint(1, 250)}", "User-Agent": "load-test"}
        async with httpx.AsyncClient(headers=headers, limits=httpx.Limits(max_connections=6)) as client:
            await story(client, "open")
            while time.monotonic() < stop:
                place = random.choice(places)
                todo = ["region"] + (["detail"] if random.random() < 0.5 else []) + \
                       (["profile"] if random.random() < 0.6 else []) + (["hovmoller"] if random.random() < 0.3 else [])
                for s in todo:
                    if time.monotonic() >= stop:
                        break
                    await story(client, f"{s} {place}")
                    await asyncio.sleep(random.uniform(5, 15))  # looking at it

    t = time.monotonic()
    await asyncio.gather(*(visitor(k) for k in range(users)))
    q = lambda v, p: sorted(v)[min(len(v) - 1, int(p * len(v)))]
    print(f"\n== {users} visitors at once, {(time.monotonic() - t) / 60:.1f} min", flush=True)
    print(f"{'request':10s} {'count':>6s} {'median':>8s} {'p95':>8s} {'max':>8s}")
    for k, v in sorted(times.items()):
        print(f"{k:10s} {len(v):6d} {q(v, .5):7.2f}s {q(v, .95):7.2f}s {max(v):7.2f}s")
    print(f"{'story':10s} {'count':>6s} {'median':>8s} {'p95':>8s} {'max':>8s}   (until its last request is answered)")
    for k, v in sorted(story_times.items()):
        print(f"{k:10s} {len(v):6d} {q(v, .5):7.2f}s {q(v, .95):7.2f}s {max(v):7.2f}s")
    print("errors:", dict(errors) or "none", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "record":
        record(sys.argv[2].rstrip("/") + "/", sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else len(PLACES))
    else:
        rec = json.load(open(sys.argv[3]))
        for users in map(int, sys.argv[4].split(",")):
            asyncio.run(replay_level(sys.argv[2], rec, users, float(sys.argv[5]) if len(sys.argv) > 5 else 4))
