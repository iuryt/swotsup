"""Screenshot a page in headless chromium. Usage (app env): screenshot.py URL PREFIX [--app]
Writes PREFIX-0.png. With --app it waits for the data, then clicks the map (PREFIX-1.png, the time series card).
Browser console errors are printed."""
import sys

from playwright.sync_api import sync_playwright

url, prefix, app = sys.argv[1], sys.argv[2], "--app" in sys.argv
CLICK = (458, 688)  # on a swath in the South Atlantic, in the opening view
with sync_playwright() as p:
    page = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]).new_page(
        viewport={"width": 1600, "height": 950})
    page.on("console", lambda m: m.type == "error" and print(f"console error: {m.text}", flush=True))
    page.on("pageerror", lambda e: print(f"page error: {e}", flush=True))
    page.goto(url, wait_until="networkidle", timeout=180_000)
    if app:
        page.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
        page.wait_for_timeout(6000)  # tiles
    page.screenshot(path=f"{prefix}-0.png")
    if app:
        page.mouse.click(*CLICK)
        page.wait_for_selector(".card .uplot, .card .loading:not(:has(.spinner))", timeout=120_000)
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{prefix}-1.png")
print("done")
