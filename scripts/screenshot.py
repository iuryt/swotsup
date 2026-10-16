"""Screenshots of the explorer in headless chromium, one per scenario (PREFIX-<scenario>.png), in order:
  open   the opening view, once the data and tiles are in
  click  click a swath on the opening globe: the card of that point
  line   zoom on the South Atlantic, draw a line across two swaths, hover its chart
  flat   switch to the flat map
Browser errors are printed. Usage (app env): screenshot.py URL PREFIX [scenario ...]   (default: open click)"""
import sys

from playwright.sync_api import sync_playwright

url, prefix, scenarios = sys.argv[1], sys.argv[2], sys.argv[3:] or ["open", "click"]
with sync_playwright() as p:
    page = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]).new_page(
        viewport={"width": 1600, "height": 950})
    page.on("console", lambda m: m.type == "error" and print(f"console error: {m.text}", flush=True))
    page.on("pageerror", lambda e: print(f"page error: {e}", flush=True))
    page.goto(url, wait_until="networkidle", timeout=180_000)
    page.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
    page.wait_for_timeout(6000)  # tiles

    def shot(name):
        page.screenshot(path=f"{prefix}-{name}.png")
        print(f"{prefix}-{name}.png", flush=True)

    for scenario in scenarios:
        if scenario == "open":
            shot("open")
        elif scenario == "click":
            page.mouse.click(655, 560)  # a swath west of Africa on the opening globe
            page.wait_for_selector(".card .hint:not(:has(.spinner))", timeout=60_000)
            print("card:", page.inner_text(".card .hint"), flush=True)
            shot("click")
            page.click(".pin .close")
        elif scenario == "line":
            page.evaluate("map.jumpTo({center: [-58, -40.5], zoom: 6})")
            page.wait_for_timeout(6000)
            page.click("#line")
            for xy in ((520, 470), (800, 520)):
                page.mouse.click(*xy)
                page.wait_for_timeout(300)
            page.mouse.dblclick(1050, 470)
            page.wait_for_selector(".card.transect .uplot, .card.transect .loading:not(:has(.spinner))", timeout=60_000)
            box = page.query_selector(".card.transect .u-over").bounding_box()
            page.mouse.move(box["x"] + box["width"] * 0.45, box["y"] + box["height"] / 2)
            page.wait_for_timeout(800)
            print("line:", page.inner_text(".card.transect .hint"), flush=True)
            shot("line")
        elif scenario == "flat":
            page.click("#projection")
            page.wait_for_timeout(5000)
            shot("flat")
print("done")
