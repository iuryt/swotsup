"""Screenshots of the explorer in headless chromium, one per scenario (PREFIX-<scenario>.png), in order:
  open   the opening view, once the data and tiles are in
  click  click a swath in the opening view (globe or, in older versions, flat map): the card of that point
  series open the time series of that point
  line   zoom on the South Atlantic, draw a line across two swaths, hover its chart
  flat   switch to the flat map
  dateline  drag a box across 180 degrees, on the globe and on the flat map (the region it gives is printed)
  phone  a phone (390x844, touch): the opening view, a tapped point and its series (small and tall), the menu sheet, a line
         drawn with taps and Done, the date buttons, a new band and moving it, and the opening view held sideways
A scenario that fails (an older version without that feature) does not stop the others. Browser errors are
printed. Usage (app env): screenshot.py URL PREFIX [scenario ...]   (default: open click series)"""
import sys

from playwright.sync_api import sync_playwright

url, prefix, scenarios = sys.argv[1], sys.argv[2], sys.argv[3:] or ["open", "click", "series"]
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    page.on("console", lambda m: m.type == "error" and print(f"console error: {m.text}", flush=True))
    page.on("pageerror", lambda e: print(f"page error: {e}", flush=True))
    page.goto(url, wait_until="networkidle", timeout=180_000)
    page.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
    page.wait_for_timeout(6000)  # tiles

    def shot(name):
        page.screenshot(path=f"{prefix}-{name}.png")
        print(f"{prefix}-{name}.png", flush=True)

    def run(scenario):
        if scenario == "open":
            shot("open")
        elif scenario == "click":  # a swath west of Africa on the globe; in the South Atlantic on the flat map
            globe = page.evaluate("document.body.classList.contains('globe')")
            page.mouse.click(*((655, 560) if globe else (458, 688)))
            page.wait_for_selector(".card", timeout=60_000)
            page.wait_for_timeout(4000)
            print("card:", page.inner_text(".card header"), flush=True)
            shot("click")
        elif scenario == "series":
            if page.query_selector(".card .expand:visible"):
                page.click(".card .expand")
            page.wait_for_selector(".card .uplot, .card .loading:not(:has(.spinner))", timeout=120_000)
            page.wait_for_timeout(1500)
            shot("series")
            if page.query_selector(".pin .close"):
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
        elif scenario == "dateline":
            for projection in ("globe", "flat"):
                if page.evaluate("document.body.classList.contains('globe')") != (projection == "globe"):
                    page.click("#projection")
                page.evaluate("map.jumpTo({center: [180, 0], zoom: 3})")
                page.wait_for_timeout(2000)
                page.click("#draw")
                page.mouse.move(650, 380)  # 180 degrees is at the middle of the map (x 800)
                page.mouse.down()
                page.mouse.move(800, 460, steps=5)
                page.mouse.move(950, 540, steps=5)
                page.mouse.up()
                page.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
                page.wait_for_timeout(5000)
                print(f"dateline box on the {projection}:", page.inner_text("#region-text"), flush=True)
                shot(f"dateline-{projection}")
            page.click("#line")  # and a line across it (on the flat map)
            page.mouse.click(660, 440)  # inside the box, across its passages
            page.wait_for_timeout(300)
            page.mouse.dblclick(940, 470)
            page.wait_for_selector(".card.transect .uplot, .card.transect .loading:not(:has(.spinner))", timeout=60_000)
            page.wait_for_timeout(1500)
            print("dateline line:", page.inner_text(".card.transect h3"), flush=True)
            shot("dateline-line")
        elif scenario == "phone":
            for size, name in (((390, 844), "phone"), ((844, 390), "phone-sideways")):
                phone = browser.new_context(viewport=dict(zip(("width", "height"), size)), has_touch=True,
                                            is_mobile=True, device_scale_factor=2).new_page()
                phone.on("pageerror", lambda e: print(f"page error (phone): {e}", flush=True))
                phone.goto(url, wait_until="networkidle", timeout=180_000)
                phone.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
                phone.wait_for_timeout(6000)
                phone.screenshot(path=f"{prefix}-{name}.png")
                print(f"{prefix}-{name}.png", flush=True)
                if name != "phone":
                    continue
                phone.touchscreen.tap(130, 520)  # a swath west of Africa on the opening globe
                phone.wait_for_selector(".card.sheet .hint:not(:has(.spinner))", timeout=60_000)
                print("phone card:", phone.inner_text(".card.sheet .hint"), flush=True)
                phone.tap(".card.sheet .expand")
                phone.wait_for_selector(".card.sheet .uplot, .card.sheet .loading:not(:has(.spinner))", timeout=120_000)
                phone.wait_for_timeout(1500)
                phone.screenshot(path=f"{prefix}-phone-series.png")
                phone.tap(".card.sheet .grab")  # tall, then small again
                phone.wait_for_timeout(800)
                phone.screenshot(path=f"{prefix}-phone-series-tall.png")
                print("phone dates after the grab bar:", phone.inner_text("#selection"), flush=True)
                phone.tap(".card.sheet .grab")
                phone.tap(".card.sheet .close")
                phone.tap("#tool-menu")
                phone.wait_for_timeout(500)
                phone.screenshot(path=f"{prefix}-phone-menu.png")
                phone.tap("#sheet-close")
                phone.tap("#tool-line")
                for xy in ((60, 470), (200, 560)):
                    phone.touchscreen.tap(*xy)
                    phone.wait_for_timeout(400)
                print("phone drawing:", phone.inner_text("#drawing-text"), flush=True)
                phone.screenshot(path=f"{prefix}-phone-drawing.png")
                phone.tap("#done")
                phone.wait_for_selector(".card.transect .uplot, .card.transect .loading:not(:has(.spinner))", timeout=60_000)
                phone.wait_for_timeout(1500)
                print("phone line:", phone.inner_text(".card.transect .hint"), flush=True)
                phone.screenshot(path=f"{prefix}-phone-line.png")
                phone.tap(".card.transect .close")
                for button in ("#next", "#next", "#more", "#more"):  # two steps later, then 3 days
                    phone.tap(button)
                    phone.wait_for_timeout(150)
                box = phone.query_selector("#timeline").bounding_box()  # a new band, dragged away from this one
                start = phone.evaluate("timeline.x(timeline.end)") + 60
                phone.mouse.move(box["x"] + start, box["y"] + 20)
                phone.mouse.down()
                phone.mouse.move(box["x"] + start + 3, box["y"] + 20, steps=3)
                phone.mouse.up()
                phone.wait_for_selector("#busy.hidden", state="attached", timeout=180_000)
                phone.wait_for_timeout(5000)
                print("phone dates:", phone.inner_text("#selection"), flush=True)
                # a finger on the band itself (on the bar, not a grip) moves it whole, keeping its length
                band = phone.evaluate("(timeline.x(timeline.start) + timeline.x(timeline.end)) / 2")
                phone.mouse.move(box["x"] + band, box["y"] + 20)
                phone.mouse.down()
                phone.mouse.move(box["x"] + band + 30, box["y"] + 20, steps=5)
                phone.mouse.up()
                phone.wait_for_timeout(500)
                print("phone band moved:", phone.inner_text("#selection"), flush=True)
                phone.screenshot(path=f"{prefix}-phone-time.png")
                print(f"{prefix}-phone-series/menu/drawing/line/time.png", flush=True)
        elif scenario == "flat":
            page.click("#projection")
            page.wait_for_timeout(5000)
            shot("flat")

    for scenario in scenarios:
        try:
            run(scenario)
        except Exception as e:
            print(f"{scenario} failed: {str(e).splitlines()[0]}", flush=True)
print("done")
