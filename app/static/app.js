// SWOT's Up: the page. The server (server.py) reads the IceChunk store and draws tiles.
const $ = (s) => document.querySelector(s);
const DAY = 86400000;
const NCEI = "https://tiles.arcgis.com/tiles/C8EMgrsFcRFL6LrL/arcgis/rest/services/{}/MapServer/tile/{z}/{y}/{x}";
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{z}/{y}/{x}";
const BASEMAPS = {
  "Bathymetry, gray": [NCEI.replace("{}", "GEBCO_grayscale_basemap_NCEI"), 0.55],  // url, brightness (dark page)
  "Bathymetry, color": [NCEI.replace("{}", "GEBCO_basemap_NCEI"), 0.75],
  "Imagery": [ESRI.replace("{}", "World_Imagery"), 0.8],
};

const state = { mode: "box", preset: "mad", symmetric: true, data: null, drawing: false, cmaps: {} };
let info, map, timeline;
try { state.cmaps = JSON.parse(localStorage.getItem("cmaps") || "{}"); } catch (e) {}
// the variable shown: its colormap (chosen in this browser, else its own), its stops, its units and kind
const cmapOf = () => (info.colormaps[state.cmaps[state.variable]] ? state.cmaps[state.variable] : info.looks[state.variable].cmap);
const stops = () => info.colormaps[cmapOf()];
const unit = () => info.looks[state.variable].units;
const isAnomaly = () => info.looks[state.variable].kind === "anomaly";

// --- small helpers
async function api(path, params = {}) {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + new URLSearchParams(params));
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const wrap = (lon) => ((lon + 180) % 360 + 360) % 360 - 180;
const near = (lon, ref) => lon + 360 * Math.round((ref - lon) / 360);  // lon as close to ref as it gets (± 360)
const deg = (v, neg, pos, digits = 2) => `${+Math.abs(v).toFixed(digits)}°${v < 0 ? neg : v > 0 ? pos : ""}`;
const lonText = (lon, digits) => deg(wrap(lon), "W", "E", digits);
const latText = (lat, digits) => deg(lat, "S", "N", digits);
const dayText = (t) => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const passages = (n) => `${n} passage${n === 1 ? "" : "s"}`;
const isoDay = (t) => new Date(t).toISOString().slice(0, 10);
const rangeText = (a, b) => {  // two days, without repeating their month and year: 4–24 Nov 2024, 28 Nov – 3 Dec 2024
  const [x, y] = [dayText(a).split(" "), dayText(b).split(" ")];
  return x[2] !== y[2] ? `${dayText(a)} – ${dayText(b)}` : x[1] !== y[1] ? `${x[0]} ${x[1]} – ${dayText(b)}` : `${x[0]}–${dayText(b)}`;
};
const timeText = (iso) => `${dayText(Date.parse(iso + "Z"))} ${iso.slice(11, 16)} UTC`;
// a phone or a short window: the options become icons, the cards sheets across the bottom (style.css)
const narrow = () => matchMedia("(max-width: 640px), (max-height: 500px)").matches;
const isGlobe = (b) => ((b.east - b.west) % 360 + 360) % 360 === 0 && b.south <= -78 && b.north >= 78;
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("#toast").classList.add("hidden"), 6000);
}

// --- loading data for the region and dates
// --- up close on the whole globe, the view read is the area in view (with a margin), followed as the map moves:
// the whole globe's 21 days take a minute, the area on screen seconds; farther out than AREA_ZOOM, the globe
const AREA_ZOOM = 4;
// the whole globe on dates the daily rasters have: one view at every zoom (no area in view, #98)
const fromRasters = () => !!(info.rasters && info.looks[state.variable].rasters && isGlobe(state.box)
  && state.start >= Date.parse(info.rasters.first) && state.start + (state.days - 1) * DAY <= Date.parse(info.rasters.last));
function areaWanted() {
  if (!map || !isGlobe(state.box) || map.getZoom() < AREA_ZOOM || fromRasters()) return state.box;
  const b = map.getBounds(), [w, e, s, n] = [b.getWest(), b.getEast(), b.getSouth(), b.getNorth()];
  const dx = (e - w) * 0.5, dy = (n - s) * 0.5;  // half the view more on each side: small moves read nothing
  let area = { west: Math.floor(w - dx), east: Math.ceil(e + dx), south: Math.max(Math.floor(s - dy), -90), north: Math.min(Math.ceil(n + dy), 90) };
  if (transect) {  // a drawn line stays inside what is read
    const xs = transect.points.map(([x]) => x), ys = transect.points.map(([, y]) => y);
    area = { west: Math.min(area.west, Math.floor(Math.min(...xs))), east: Math.max(area.east, Math.ceil(Math.max(...xs))),
             south: Math.min(area.south, Math.floor(Math.min(...ys))), north: Math.max(area.north, Math.ceil(Math.max(...ys))) };
  }
  if (area.east - area.west >= 300) return state.box;  // most of the world: the globe
  const wrap180 = (x) => ((x + 180) % 360 + 360) % 360 - 180;
  return { west: wrap180(area.west), east: wrap180(area.east), south: area.south, north: area.north };
}
function areaHolds(area, inner) {  // whether an area holds another (longitudes continuous from the area's west)
  if (isGlobe(area)) return true;
  if (isGlobe(inner)) return false;
  const span = ((area.east - area.west) % 360 + 360) % 360, at = (x) => ((x - area.west) % 360 + 360) % 360;
  const w = at(inner.west), e = w + (((inner.east - inner.west) % 360 + 360) % 360);
  return w <= span && e <= span && inner.south >= area.south && inner.north <= area.north;
}
function followArea() {  // once the map stops: read another area if the view left the one read (or the globe)
  clearTimeout(followArea.timer);
  followArea.timer = setTimeout(() => {
    if (!state.data || !isGlobe(state.box)) return;
    const want = areaWanted(), b = map.getBounds();
    const seen = { west: b.getWest(), east: b.getEast(), south: Math.max(b.getSouth(), -90), north: Math.min(b.getNorth(), 90) };
    if (isGlobe(want) ? !isGlobe(state.area) : (isGlobe(state.area) || !areaHolds(state.area, seen))) loadData();
  }, 1000);
}

// the variable's entry in the menu: its rivers-and-lakes, mean-sea-surface and calibration choices are in the
// Display menu
function showVariable() {
  let k = state.variable;
  if (info.looks[k].twin && info.looks[k].water) k = info.looks[k].twin;
  if (info.looks[k].mss && !info.looks[k].dtu) k = info.looks[k].mss;
  if (info.looks[k].cal && !info.looks[k].calibrated) k = info.looks[k].cal;
  $("#variable").value = k;
}

async function loadData() {
  // only the latest choice counts: a view asked before and answered later (a slow read) must not replace it
  const mine = (loadData.n = (loadData.n ?? 0) + 1), latest = () => mine === loadData.n;
  $("#busy").classList.remove("hidden");
  state.area = areaWanted();
  // every later request (tiles, values, download) names this view, so the server can read it again
  const view = new URLSearchParams({ ...state.area, start: isoDay(state.start), days: state.days, variable: state.variable });
  // a view too big to keep is read in batches: how far, while it lasts
  const where = isGlobe(state.area) ? "the whole globe" : isGlobe(state.box) ? "the area in view" : "the region";
  const poll = setInterval(async () => {
    const p = await api("/api/progress", view).catch(() => ({}));
    if (p.total && latest()) showProgress("view", `Reading ${state.days} day${state.days > 1 ? "s" : ""} of ${where} from the cloud`,
                              p.done / p.total, `${p.done} of ${p.total} passages · ${Math.round(p.seconds)} s`);
  }, 1000);
  try {
    const d = await api("/api/load", view);
    if (!latest()) return;
    state.data = d;
    state.view = view.toString();
    state.screen = null;  // the whole view's colors first, then the screen's
    $("#stats").innerHTML = `<b>${d.passages}</b> passages · <b>${d.pixels >= 1e5 ? `${(d.pixels / 1e6).toFixed(1)} M` : `${Math.round(d.pixels / 1e3)} k`}</b> pixels · ` +
      (d.seconds ? `read from the cloud in <b>${d.seconds.toFixed(1)} s</b>` : "from the server's memory");
    state.code = d.code;
    if ($("#download")) $("#download").href = `/api/netcdf?${state.view}`;  // absent where SWOT_NETCDF=0
    setColors();
    fitColors();
    const after = state.afterLoad;  // a link's point and line, once its view is in
    state.afterLoad = null;
    after?.();
    linkSoon();
  } catch (e) {
    if (latest()) toast(e.message);
  } finally {
    clearInterval(poll);
    if (latest()) {
      showProgress("view", null);
      $("#busy").classList.add("hidden");
    }
  }
}

// the progress bar under the legend's title: of a view read in batches, or of the 250 m tiles in view (`kind`);
// without a text, it goes (if it is still that kind's)
function showProgress(kind, text, fraction, count) {
  const el = $("#progress");
  if (text == null) {
    if (el.dataset.kind === kind) el.classList.add("hidden");
    return;
  }
  el.dataset.kind = kind;
  el.classList.remove("hidden");
  $("#progress .progress-text").textContent = text;
  $("#progress .progress-bar i").style.width = `${(100 * fraction).toFixed(1)}%`;
  $("#progress .progress-count").textContent = count;
}

// the 250 m tiles in view: how many were asked and how many came (or were dropped); a bar while some are coming
// the rasters under the passages' tiles (from the map's zoom 6) only cover what is loading: they go once the map is
// idle (every tile in view loaded and drawn, nothing moving: "loaded" alone came between a zoom's old tiles and its
// new ones, and they went before those came), and come back as soon as it moves or asks for new ones
function watchUnder() {
  const show = (on) => {
    const under = showTiles.now?.under;
    if (!under || !map.getLayer(under.id) || watchUnder.on === on) return;
    watchUnder.on = on;
    // fading out over 0.4 s (its ~10 km pixels at the swaths' edges go: at once, the swaths seemed to jump), back at once
    map.setPaintProperty(under.id, "raster-opacity-transition", { duration: on ? 0 : 400 });
    map.setPaintProperty(under.id, "raster-opacity", on ? 1 : 0);
    dbg({ ev: "under", shown: on });
  };
  map.on("idle", () => { const swot = showTiles.now?.swot; if (map.getZoom() >= 6 && swot && map.isSourceLoaded(swot.id)) show(false); });
  map.on("movestart", () => show(true));
  map.on("dataloading", (e) => e.sourceId && e.sourceId === showTiles.now?.swot?.id && show(true));
}

function watchDetail() {
  let asked = new Set(), came = new Set();
  const update = () => {
    for (const k of asked) if (!map.getSource(k.split("/")[0])) { asked.delete(k); came.delete(k); }  // layers swapped out
    if (came.size >= asked.size) { asked = new Set(); came = new Set(); showProgress("detail", null); return; }
    showProgress("detail", "Reading the 250 m here from the cloud", came.size / asked.size, `${came.size} of ${asked.size} tiles`);
  };
  const id = (e) => e.sourceId?.startsWith("detail-") && e.tile ? `${e.sourceId}/${e.tile.tileID.key}` : null;
  map.on("dataloading", (e) => { const k = id(e); if (k != null) { asked.add(k); update(); } });
  for (const done of ["sourcedata", "dataabort", "error"]) {
    map.on(done, (e) => { const k = id(e); if (k != null && asked.has(k) && (done !== "sourcedata" || e.tile.state === "loaded"
      || e.tile.state === "errored")) { came.add(k); update(); } });
  }
}

// --- the colormap, chosen in the Display menu: a strip and its name for each; the variable's kind first
// (diverging for anomalies, sequential for levels); remembered per variable in this browser
function cmapMenu() {
  const strip = (name) => `<i style="background:linear-gradient(90deg, ${info.colormaps[name].join(", ")})"></i>`;
  const own = info.looks[state.variable].cmap, names = Object.keys(info.colormaps);
  const diverging = names.filter((n) => info.diverging.includes(n)), sequential = names.filter((n) => !info.diverging.includes(n));
  const groups = isAnomaly() ? [diverging, sequential] : [sequential, diverging];
  const item = (n) => `<button data-cmap="${n}" class="${n === cmapOf() ? "active" : ""}">${strip(n)}<span>${n}${n === own ? " (default)" : ""}</span></button>`;
  $("#cmap .cmap-current").innerHTML = `${strip(cmapOf())}<span>${cmapOf()}</span>`;
  $("#cmap .cmap-list").innerHTML = groups.map((g) => g.map(item).join("")).join("<hr>");
}

function setupCmap() {
  $("#cmap .cmap-current").onclick = () => $("#cmap .cmap-list").classList.toggle("hidden");
  $("#cmap .cmap-list").onclick = (e) => {
    const b = e.target.closest("[data-cmap]");
    if (!b) return;
    state.cmaps[state.variable] = b.dataset.cmap;
    try { localStorage.setItem("cmaps", JSON.stringify(state.cmaps)); } catch (err) {}
    $("#cmap .cmap-list").classList.add("hidden");
    setColors();
    if (transect?.kind === "hovmoller") fillHovmoller();
  };
}

// --- robust colors of what is on screen: once the map stops, the median ± 2 x 1.4826 x MAD of the pixels in view;
// taken if they moved by more than ~15 % of the range (a new range draws the tiles again), so red and blue show at
// any zoom
async function fitColors() {
  clearTimeout(fitColors.timer);
  fitColors.timer = setTimeout(async () => {
    if (state.preset !== "mad" || !state.data) return;
    // only after a real change of what is on screen since the colors were last fitted: a zoom level, or half a screen
    const here = { z: map.getZoom(), c: map.getCenter() }, last = fitColors.at;
    if (last && Math.abs(here.z - last.z) < 1 && map.project(last.c).dist(map.project(here.c)) < map.getCanvas().clientWidth / 2) return;
    const b = map.getBounds(), view = state.view;
    const r = await api(`/api/limits?${view}`, { lon0: wrap(b.getWest()), lon1: wrap(b.getEast()),
      lat0: Math.max(b.getSouth(), -90), lat1: Math.min(b.getNorth(), 90) }).catch(() => null);
    if (!r?.[state.mode] || state.view !== view || state.preset !== "mad") return;
    const [lo, hi] = r[state.mode], span = Math.max(state.vmax - state.vmin, 1e-9);
    state.screen = r;
    // new colors redraw every tile on screen: only for a change worth it (a 25 % move of the limits)
    if (Math.abs(lo - state.vmin) > 0.25 * span || Math.abs(hi - state.vmax) > 0.25 * span) { fitColors.at = here; setColors(); }
  }, 600);
}

// --- colors: from percentiles of the loaded values, or typed
function setColors() {
  const q = state.data?.quantiles[state.mode] ?? [];
  // robust: the median ± 2 x 1.4826 x MAD of what is on screen (fitColors), else of the whole view; else percentiles
  const robust = state.screen?.[state.mode] ?? state.data?.robust?.[state.mode];
  if (state.preset !== "custom" && q.length) {
    const p = Number(state.preset) * 10;
    let [lo, hi] = state.preset === "mad" && robust ? robust : [q[p], q[1000 - p]];
    // about zero once a mean is subtracted; with nothing subtracted, robust colors stay about the median
    if (state.symmetric && !(state.preset === "mad" && state.mode === "none")) [lo, hi] = [-Math.max(-lo, hi), Math.max(-lo, hi)];
    [state.vmin, state.vmax] = [+lo.toFixed(3), +hi.toFixed(3)];
  }
  $("#colorbar").style.background = `linear-gradient(90deg, ${stops().join(", ")})`;
  $("#vmin").value = state.vmin;
  $("#vmax").value = state.vmax;
  $("#vmin-text").textContent = `${state.vmin} ${unit()}`;
  $("#vmax-text").textContent = `${state.vmax} ${unit()}`;
  $("#vmin-label").textContent = `Min (${unit()})`;
  $("#vmax-label").textContent = `Max (${unit()})`;
  const middle = (state.vmin + state.vmax) / 2;  // 0 about zero; the median's value when centred on it
  $("#zero-text").textContent = Math.abs(middle) < 5e-4 ? "0" : `${+middle.toFixed(3)}`;
  const look = info.looks[state.variable];
  $("#water-row").classList.toggle("hidden", !look.twin);  // rivers and lakes: where the variable has the choice
  $("#water").checked = look.water;
  $("#mss-row").classList.toggle("hidden", !look.mss);  // which mean sea surface: where the variable has the choice
  $("#cal-row").classList.toggle("hidden", !look.cal);  // calibrated or not: likewise
  $("#cal").checked = look.calibrated;
  document.querySelectorAll("#mss button").forEach((b) => b.classList.toggle("active", (b.dataset.value === "dtu") === look.dtu));
  cmapMenu();
  const pct = (v) => { // percentile of a value, from the quantiles
    let i = q.findIndex((x) => x >= v);
    return i < 0 ? 100 : i / 10;
  };
  $("#percentiles").innerHTML = !q.length ? "" : state.preset === "custom"  // fixed limits: say so, and the way back
    ? `custom limits (at percentiles ${pct(state.vmin)} and ${pct(state.vmax)}) · <a href="#" id="to-robust">use Robust</a>`
    : `limits at percentiles ${pct(state.vmin)} and ${pct(state.vmax)}${state.screen ? " · of the screen" : ""}`;
  $("#to-robust")?.addEventListener("click", (e) => { e.preventDefault(); state.preset = "mad"; setColors(); fitColors(); });
  $("#legend-variable").textContent = info.variables[state.variable] + (state.mode === "none" ? "" :
    state.mode === "box" ? (isGlobe(state.box) && state.area && !isGlobe(state.area) ? ", minus the mean of the area in view" : ", minus box mean")
    : ", minus passage means");
  document.querySelectorAll("#preset button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.preset));
  document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.mode));
  $("#symmetric").checked = state.symmetric;
  linkSoon();
  if (!state.data) return;
  if (transect && transect.key !== lineKey()) fillLine();
  // swot://: the page colors the tiles (`colorTile`): new colors read no tile again
  const url = `swot://api/tile/{z}/{x}/{y}.png?${state.view}&v=${info.version}&mode=${state.mode}&vmin=${state.vmin}&vmax=${state.vmax}&cmap=${cmapOf()}`;
  const raster = { "raster-fade-duration": 0, "raster-resampling": "nearest" };
  const credit = { attribution: info.looks[state.variable].dtu ? "MSS: DTU25 (DTU Space, CC BY 4.0)" : undefined };
  // the whole globe on dates the daily rasters have: their tiles (to zoom 6, drawn larger beyond) always underneath,
  // the passages' only from zoom 7: up close, a place not read yet shows the rasters, never nothing
  // (one layer at every zoom: from 6, under the passages', it goes once all is loaded, `watchUnder`)
  const under = fromRasters();
  if (under) {
    if (showTiles.now?.under?.url !== url) watchUnder.on = true;  // a new layer starts shown
    showTiles("under", url, { paint: raster }, { ...credit, maxzoom: 6 });
  }
  else dropTiles("under");
  // (the source's tiles from 7; the layer shows from the map's zoom 6, where MapLibre already takes tiles of 7)
  const base = under ? { minzoom: 6 } : {}, tiles = under ? { minzoom: 7 } : {};
  if (!info.detail) return showTiles("swot", url, { paint: raster, ...base }, { ...credit, ...tiles });
  // the finer product over it, from a zoom on: it fades in over half a zoom level, over the base, which stays: under
  // it the server draws the base inside its swath (no 2 km at its edges), so where it has no tile yet the base shows
  const z = info.detail.zoom;
  showTiles("swot", url, { paint: raster, ...base }, { ...credit, ...tiles });
  showTiles("detail", url.replace("/api/tile/", "/api/detail/"), { minzoom: z - 1, paint: { ...raster,
    "raster-opacity": ["interpolate", ["linear"], ["zoom"], z - 1, 0, z - 0.4, 1] } }, { minzoom: z - 1 });
}

// --- ?debug=1 (a test server): the map's events go to the server (/api/debug), to see what a browser drew and when
const DEBUG = new URLSearchParams(location.search).get("debug") === "1", debugLog = [];
const dbg = (e) => DEBUG && debugLog.push({ t: Math.round(performance.now()), ...e });
function setupDebug() {
  const ours = (id) => id && /^(swot|detail|under)-/.test(id);
  const tile = (e) => ({ s: e.sourceId, k: e.tile.tileID.key, ...e.tile.tileID.canonical });  // z, x, y
  map.on("dataloading", (e) => ours(e.sourceId) && e.tile && dbg({ ev: "ask", ...tile(e), moving: map.isMoving() }));
  map.on("sourcedata", (e) => ours(e.sourceId) && e.tile && dbg({ ev: "tile", ...tile(e), st: e.tile.state }));
  map.on("dataabort", (e) => ours(e.sourceId) && e.tile && dbg({ ev: "abort", ...tile(e) }));
  map.on("error", (e) => dbg({ ev: "error", s: e.sourceId, msg: String(e.error?.message ?? e.error) }));
  map.on("moveend", () => dbg({ ev: "moveend", z: +map.getZoom().toFixed(2), c: map.getCenter().toArray().map((v) => +v.toFixed(3)) }));
  map.on("idle", () => dbg({ ev: "idle" }));  // every tile in view drawn: a move's end to here is its whole wait
  // where each request's time goes, from the browser itself: its queue, the answer, the download, the HTTP cache
  // (transferSize 0), the protocol, and the server's own time (Server-Timing)
  performance.setResourceTimingBufferSize?.(10000);
  new PerformanceObserver((list) => {
    for (const r of list.getEntries()) {
      if (!r.name.includes("/api/") || r.name.includes("/api/debug")) continue;
      dbg({ ev: "net", path: new URL(r.name).pathname.split("/").slice(0, 3).join("/"), z: +(r.name.match(/\/(\d+)\/\d+\/\d+\.png/)?.[1] ?? -1),
            stall: Math.round(r.requestStart - r.startTime), ttfb: Math.round(r.responseStart - r.requestStart),
            down: Math.round(r.responseEnd - r.responseStart), bytes: r.transferSize, proto: r.nextHopProtocol,
            server: r.serverTiming?.[0]?.duration ?? null });
    }
  }).observe({ type: "resource", buffered: true });
  // the page itself: tasks that held it more than 50 ms, and frames slower than that while the map moves
  try { new PerformanceObserver((list) => { for (const t of list.getEntries()) dbg({ ev: "longtask", ms: Math.round(t.duration) }); }).observe({ type: "longtask" }); } catch {}
  let last = performance.now(), slow = 0, frames = 0;
  const frame = (t) => { if (map.isMoving()) { frames++; if (t - last > 50) slow++; } last = t; requestAnimationFrame(frame); };
  requestAnimationFrame(frame);
  dbg({ ev: "client", dpr: devicePixelRatio, w: innerWidth, h: innerHeight, net: navigator.connection?.effectiveType,
        downlink: navigator.connection?.downlink, ua: navigator.userAgent.match(/(Chrome|Firefox|Safari)\/[\d.]+/)?.[0] });
  setInterval(() => {
    const flying = [...VALUES.values()].filter((v) => !v.done).length;
    dbg({ ev: "layers", ids: map.getStyle().layers.map((l) => l.id).filter(ours), z: +map.getZoom().toFixed(2),
          values: VALUES.size, flying, heap: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e6) : null,
          frames, slow });
    frames = slow = 0;
    if (debugLog.length) fetch("/api/debug", { method: "POST", body: JSON.stringify({ sent: Date.now(), origin: performance.timeOrigin, events: debugLog.splice(0) }) }).catch(() => {});
  }, 2000);
}

// --- tiles colored here: the server sends each pixel's value (24 bits in red, green and blue, VALUE_SCALE units; the
// alpha says where there is data), fetched once (the browser keeps it), and the colormap and limits are applied here
const VALUE_SCALE = 1e4, LUTS = {};
function tileColors(name) {  // 256 colors interpolated from the colormap's 16 stops
  if (LUTS[name]) return LUTS[name];
  const stops = info.colormaps[name].map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
  const out = new Uint8Array(256 * 3);
  for (let k = 0; k < 256; k++) {
    const f = k / 255 * (stops.length - 1), i = Math.min(Math.floor(f), stops.length - 2), t = f - i;
    for (let c = 0; c < 3; c++) out[k * 3 + c] = Math.round(stops[i][c] * (1 - t) + stops[i + 1][c] * t);
  }
  return (LUTS[name] = out);
}
// the value tiles fetched, kept decoded (and those on their way, shared): new colors repaint from memory, and a tile is
// never asked twice (VALUES_KEPT: ~65 MB). A tile on its way is cancelled when no layer waits for it any more (the
// map moved on): a browser asks a server only a few at once, and old ones would hold the new ones back
const VALUES = new Map(), VALUES_KEPT = 250;
async function fetchValues(url, signal) {
  const t0 = performance.now();
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${res.status} ${new URL(url).pathname}`);
  const blob = await res.blob(), t1 = performance.now();
  const bmp = await createImageBitmap(blob, { premultiplyAlpha: "none", colorSpaceConversion: "none" });
  const ctx = new OffscreenCanvas(bmp.width, bmp.height).getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  const img = ctx.getImageData(0, 0, bmp.width, bmp.height);
  // ?debug=1: the answer's time (the server's own from its Server-Timing; the rest is the network and the queue of
  // requests to it) and the decoding's
  img.timing = { answer: Math.round(t1 - t0), server: Number((res.headers.get("Server-Timing") ?? "").split("dur=")[1]) || null,
                 decode: Math.round(performance.now() - t1), bytes: blob.size };
  return img;
}
async function colorTile(params, abort) {
  const u = new URL("/" + params.url.slice("swot://".length), location.href), q = u.searchParams;
  const [vmin, vmax, cmap] = [Number(q.get("vmin")), Number(q.get("vmax")), q.get("cmap")];
  for (const k of ["vmin", "vmax", "cmap"]) q.delete(k);
  q.set("values", "1");
  const key = u.toString(), tCall = performance.now();
  let got = VALUES.get(key);
  const how = !got ? "fetched" : got.done ? "memory" : "shared";  // asked of the server here, kept, or on its way
  if (got) VALUES.delete(key);  // most recent last
  else {
    const ctl = new AbortController();
    got = { ctl, waiting: 0, done: false };
    got.values = fetchValues(key, ctl.signal).then((v) => { got.done = true; return v; })
      .catch((e) => { if (VALUES.get(key) === got) VALUES.delete(key); throw e; });
  }
  VALUES.set(key, got);
  for (const [old, o] of VALUES) { if (VALUES.size <= VALUES_KEPT) break; if (o.done) VALUES.delete(old); }
  got.waiting++;
  let left = false;
  const leave = () => {
    if (left) return;
    left = true;
    if (--got.waiting === 0 && !got.done) { got.ctl.abort(); if (VALUES.get(key) === got) VALUES.delete(key); }
  };
  abort?.signal.addEventListener("abort", leave, { once: true });
  let values;
  try { values = await got.values; } finally { abort?.signal.removeEventListener("abort", leave); if (!got.done) leave(); else if (!left) { left = true; got.waiting--; } }
  const tValues = performance.now();
  const img = new ImageData(new Uint8ClampedArray(values.data), values.width, values.height);
  const p = img.data, colors = tileColors(cmap), scale = 255 / (vmax - vmin);
  for (let i = 0; i < p.length; i += 4) {
    if (!p[i + 3]) continue;
    const v = (((p[i] << 16) | (p[i + 1] << 8) | p[i + 2]) - 8388608) / VALUE_SCALE;
    const k = Math.max(0, Math.min(255, Math.round((v - vmin) * scale))) * 3;
    p[i] = colors[k]; p[i + 1] = colors[k + 1]; p[i + 2] = colors[k + 2];
  }
  const data = await createImageBitmap(img);  // MapLibre takes it as it is: no PNG encoded here and decoded there
  if (DEBUG) {
    const [z, x, y] = u.pathname.match(/(\d+)\/(\d+)\/(\d+)\.png$/).slice(1).map(Number);
    dbg({ ev: "timing", kind: u.pathname.includes("/detail/") ? "detail" : "swot", z, x, y, how,
          wait: Math.round(tValues - tCall), ...(how === "fetched" ? values.timing : {}), color: Math.round(performance.now() - tValues) });
  }
  return { data };
}
maplibregl.addProtocol("swot", colorTile);
// more images at once than MapLibre's 16: the basemap's (another server, sometimes slow) would hold the data's back
maplibregl.setMaxParallelImageRequests(64);

// new tiles (other colors, another view) come as a new layer over the old one, which goes once the new one has its
// tiles: the map never goes blank in between (setting a source's tiles drops every tile on screen first)
function showTiles(kind, url, layer, source = {}) {
  const now = (showTiles.now ??= {}), cur = now[kind];
  if (cur?.url === url) {  // the same tiles: only how they show
    for (const [k, v] of Object.entries(layer.paint)) map.setPaintProperty(cur.id, k, v);
    return;
  }
  const id = `${kind}-${(showTiles.n = (showTiles.n ?? 0) + 1)}`, order = map.getStyle().layers.map((l) => l.id);
  dbg({ ev: "layer+", id, replaces: cur?.id, url: url.split("?")[1]?.replace(/west=.*?&variable=/, "") });
  const first = (...kinds) => kinds.map((k) => order.find((l) => l.startsWith(`${k}-`))).find(Boolean);
  const before = cur ? order[order.indexOf(cur.id) + 1]
    : { under: first("swot", "detail"), swot: first("detail") }[kind] ?? "box";
  map.addSource(id, { type: "raster", tiles: [url], tileSize: 256, ...source });
  map.addLayer({ type: "raster", ...layer, id, source: id }, before);
  now[kind] = { id, url };
  if (!cur) return;
  const remove = (old) => {
    dbg({ ev: "layer-", id: old });
    if (map.getLayer(old)) map.removeLayer(old);
    if (map.getSource(old)) map.removeSource(old);
  };
  // the old layer stays until the new one has its tiles, however long; a newer one meanwhile replaces the one still
  // loading (the old stays under it): two at most
  const waiting = (showTiles.waiting ??= {})[kind];
  if (waiting) {
    waiting.next = id;  // before the removal: it fires sourcedata for the removed one
    remove(cur.id);
    return;
  }
  const entry = (showTiles.waiting[kind] = { old: cur.id, next: id });
  const loaded = (e) => {
    if (e.sourceId !== entry.next || !map.getSource(entry.next) || !map.isSourceLoaded(entry.next)) return;
    map.off("sourcedata", loaded);
    delete showTiles.waiting[kind];
    remove(entry.old);
  };
  map.on("sourcedata", loaded);
}

function dropTiles(kind) {  // a kind of tiles no longer shown (and its layer still waiting to go, if any)
  const now = showTiles.now ?? {}, waiting = showTiles.waiting?.[kind];
  for (const id of [now[kind]?.id, waiting?.old]) {
    if (id && map.getLayer(id)) map.removeLayer(id);
    if (id && map.getSource(id)) map.removeSource(id);
  }
  delete now[kind];
  if (showTiles.waiting) delete showTiles.waiting[kind];
}

// --- the dates: from the timeline, or a click on a time series
function setDates(start, days) {
  clearTimeout(nudge.timer);  // a drag after the buttons wins over what they left pending
  Object.assign(state, { start, days });
  timeline.setBand(start, days);
  if (transect?.kind === "hovmoller") transect.card.plot?.redraw();  // its band of the selected dates
  pointCard?.plot?.redraw();  // its shaded band
  loadData();
}

// --- the dates with a finger: move by the selection's length, a day less or more, or a day from the calendar.
// The band moves at once; the data are read once the taps stop.
function nudge(start, days) {
  const [lo, hi] = timeline.full;
  days = Math.min(Math.max(days, 1), info.max_days);
  start = Math.min(Math.max(start, lo), hi - days * DAY);
  Object.assign(state, { start, days });
  timeline.setBand(start, days);
  clearTimeout(nudge.timer);
  nudge.timer = setTimeout(() => setDates(start, days), 500);
}

function setupDateButtons() {
  $("#prev").onclick = () => nudge(state.start - state.days * DAY, state.days);
  $("#next").onclick = () => nudge(state.start + state.days * DAY, state.days);
  $("#fewer").onclick = () => nudge(state.start, state.days - 1);
  $("#more").onclick = () => nudge(state.start, state.days + 1);
  const pick = $("#pick");
  [pick.min, pick.max] = [isoDay(timeline.full[0]), isoDay(timeline.full[1] - DAY)];
  $("#selection").onclick = () => {
    if (!narrow()) return;
    pick.value = isoDay(state.start);
    try { pick.showPicker(); } catch (e) { pick.focus(); pick.click(); }
  };
  pick.onchange = () => { if (pick.value) nudge(Date.parse(pick.value + "T00:00:00Z"), state.days); };
}

// --- the region
function setBox(box, reload = true) {
  state.box = box;
  const globe = isGlobe(box);
  $("#globe").classList.toggle("hidden", globe);  // a way back, once a box is chosen
  $("#region-text").textContent = globe ? "Whole globe" :
    `${lonText(box.west)} to ${lonText(box.east)}, ${latText(box.south)} to ${latText(box.north)}`;
  $("#region-button").title = `${$("#region-text").textContent} (click to type it)`;
  for (const k of ["west", "east", "south", "north"]) $("#" + k).value = box[k];
  let { west, east } = box;
  if (east <= west) east += 360;  // continuous across 180
  const ring = [[west, box.south], [east, box.south], [east, box.north], [west, box.north], [west, box.south]];
  map.getSource("box").setData({ type: "Feature", geometry: { type: globe ? "Point" : "LineString", coordinates: globe ? [0, 0] : ring } });
  api("/api/timeline", box).then((d) => timeline.setTicks(d));
  if (reload) loadData();
}

function drawBox() {  // the next drag on the map draws the region
  if (state.lining) drawLine();
  state.drawing = !state.drawing;
  map.getCanvas().style.cursor = state.drawing ? "crosshair" : "";
  for (const handler of [map.dragPan, map.touchZoomRotate]) state.drawing ? handler.disable() : handler.enable();
  showDrawing();
}

function showDrawing() {  // the bar while drawing, and the buttons that started it
  const mode = state.drawing ? "box" : state.lining ? "line" : null;
  $("#drawing").classList.toggle("hidden", !mode);
  for (const id of ["#draw", "#tool-box"]) $(id).classList.toggle("active", mode === "box");
  for (const id of ["#line", "#tool-line"]) $(id).classList.toggle("active", mode === "line");
  if (!mode) return;
  const n = state.vertices?.length ?? 0;
  $("#drawing-text").textContent = mode === "box" ? "Drag on the map to draw a box"
    : n < 2 ? `${narrow() ? "Tap" : "Click"} points on the map${n ? " (one more at least)" : ""}`
    : `${n} points · add more, or Done`;
  $("#undo").classList.toggle("hidden", mode !== "line" || !n);
  $("#done").classList.toggle("hidden", mode !== "line" || n < 2);
  document.body.classList.remove("sheet");
}

function setupDrawing() {
  let start = null;
  // the globe gives longitudes in -180..180, so a drag across 180 jumps by 360: take the end the short way
  // from the start; the flat map's are already continuous (and a box there may be wider than 180)
  const lng = (e) => document.body.classList.contains("globe") ? near(e.lngLat.lng, start.lng) : e.lngLat.lng;
  let last = null;  // a touch's end has no position: the last one it moved to
  map.on("mousedown", (e) => { if (state.drawing) start = e.lngLat; });
  map.on("touchstart", (e) => { if (state.drawing && e.points.length === 1) start = last = e.lngLat; });
  map.on("touchmove", (e) => { if (start) { last = e.lngLat; map.fire("mousemove", { lngLat: e.lngLat }); } });
  map.on("touchend", () => {
    if (!start) return;
    map.fire("mouseup", { lngLat: last });
    state.justDrew = false;  // no click follows a touch drag
  });
  map.on("mousemove", (e) => {
    if (!start) return;
    const [w, e2, s, n] = [Math.min(start.lng, lng(e)), Math.max(start.lng, lng(e)),
                           Math.min(start.lat, e.lngLat.lat), Math.max(start.lat, e.lngLat.lat)];
    map.getSource("box").setData({ type: "Feature", geometry: { type: "LineString",
      coordinates: [[w, s], [e2, s], [e2, n], [w, n], [w, s]] } });
  });
  map.on("mouseup", (e) => {
    if (!start) return;
    const r = (v) => Math.round(v * 100) / 100;
    const box = { west: r(wrap(Math.min(start.lng, lng(e)))), east: r(wrap(Math.max(start.lng, lng(e)))),
                  south: r(Math.min(start.lat, e.lngLat.lat)), north: r(Math.max(start.lat, e.lngLat.lat)) };
    start = null;
    state.justDrew = true;  // the click that ends the drag is not a click for a time series
    drawBox();
    if (box.west !== box.east && box.south !== box.north) setBox(box);
  });
}

// --- the timeline: whole mission, a tick on each day SWOT passed over the region, an orange band for the dates
class Timeline {
  constructor(canvas, first, last, onChange) {
    Object.assign(this, { canvas, onChange, ticks: { day: [], n: [], cycle: [] }, pointers: new Map() });
    this.full = [Math.floor(first / DAY) * DAY, Math.ceil(last / DAY) * DAY + DAY];
    this.view = [...this.full];
    canvas.addEventListener("pointerdown", (e) => this.down(e));
    canvas.addEventListener("pointermove", (e) => this.move(e));
    canvas.addEventListener("pointerup", (e) => this.up(e));
    canvas.addEventListener("pointercancel", (e) => this.up(e));
    canvas.addEventListener("pointerleave", () => { $("#readout").textContent = ""; });
    canvas.addEventListener("wheel", (e) => this.zoom(e), { passive: false });
    new ResizeObserver(() => this.draw()).observe(canvas);
  }
  setTicks(t) { this.ticks = t; this.draw(); }
  setBand(start, days) { [this.start, this.end] = [start, start + days * DAY]; this.draw(); }
  x(t) { return (t - this.view[0]) / (this.view[1] - this.view[0]) * this.canvas.clientWidth; }
  t(x) { return this.view[0] + x / this.canvas.clientWidth * (this.view[1] - this.view[0]); }
  day(x) { return Math.round(this.t(x) / DAY) * DAY; }
  draw() {
    const c = this.canvas, ctx = c.getContext("2d"), dpr = devicePixelRatio || 1, w = c.clientWidth, h = c.clientHeight;
    [c.width, c.height] = [w * dpr, h * dpr];
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    this.touch = narrow();  // a finger: no grips; buttons change the length (see down)
    const bar = [4, h - 20];  // top, bottom of the ticks
    ctx.fillStyle = "rgba(148,163,184,0.08)";
    ctx.fillRect(this.x(this.full[0]), bar[0], this.x(this.full[1]) - this.x(this.full[0]), bar[1] - bar[0]);
    ctx.fillStyle = "#64748b";
    const width = Math.max(1, (this.x(DAY) - this.x(0)) * 0.7);
    for (const d of this.ticks.day) {
      const x = this.x(d);
      if (x > -width && x < w) ctx.fillRect(x, bar[0], width, bar[1] - bar[0]);
    }
    // months along the bottom, as many as fit
    ctx.fillStyle = "#94a3b8";
    ctx.font = "11px Inter, sans-serif";
    ctx.textAlign = "center";
    const perMonth = this.x(30 * DAY) - this.x(0), step = [1, 2, 3, 6, 12].find((s) => s * perMonth > 70) ?? 12;
    const perDay = this.x(DAY) - this.x(0);
    if (perMonth > 2 * w) {  // a few weeks only (a store being filled): days, as many as fit
      const every = [1, 2, 5, 7, 14].find((s) => s * perDay > 60) ?? 14;
      for (let t = Math.ceil(this.view[0] / DAY) * DAY; t < this.view[1]; t += every * DAY) {
        const x = this.x(t);
        if (x < 15 || x > w - 15) continue;
        ctx.fillRect(x, bar[1], 1, 4);
        ctx.fillText(new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }), x, h - 3);
      }
    }
    const d = new Date(perMonth > 2 * w ? 8.64e15 : this.view[0]);  // (no months then)
    for (let m = new Date(Date.UTC(d.getUTCFullYear(), 0, 1)); m < this.view[1]; m.setUTCMonth(m.getUTCMonth() + step)) {
      const x = this.x(m.getTime());
      if (x < 15 || x > w - 15) continue;
      ctx.fillRect(x, bar[1], 1, 4);
      ctx.fillText(m.toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" }), x, h - 3);
    }
    // the selected days
    const [a, b] = [this.x(this.start), this.x(this.end)];
    ctx.fillStyle = "rgba(249,115,22,0.3)";
    ctx.fillRect(a, 0, Math.max(b - a, 2), bar[1] + 2);
    ctx.fillStyle = "#f97316";
    ctx.fillRect(a - 1, 0, 3, bar[1] + 2);
    ctx.fillRect(Math.max(b, a + 2) - 2, 0, 3, bar[1] + 2);
    if (!this.touch) for (const x of [a, Math.max(b, a + 2)]) {  // grips on the edges, to drag them
      ctx.beginPath();
      ctx.roundRect(x - 4, (bar[0] + bar[1]) / 2 - 9, 8, 18, 3);
      ctx.fill();
    }
    const n = Math.round((this.end - this.start) / DAY);
    $("#selection").textContent = n > 1 ? `${rangeText(this.start, this.end - DAY)} · ${n} days` : `${dayText(this.start)} · 1 day`;
  }
  down(e) {
    this.pointers.set(e.pointerId, e.offsetX);
    this.canvas.setPointerCapture(e.pointerId);
    if (this.pointers.size === 2) {  // a second finger: pinch to zoom, instead of dragging
      const [x1, x2] = [...this.pointers.values()];
      [this.start, this.end] = [this.anchor?.start ?? this.start, this.anchor?.end ?? this.end];
      this.drag = null;
      this.pinch = { gap: Math.abs(x2 - x1) || 1, t: this.t((x1 + x2) / 2), view: [...this.view] };
      return this.draw();
    }
    const x = e.offsetX, [a, b] = [this.x(this.start), this.x(this.end)];
    // a finger on the band (or 20 px around it, a day is a pixel) moves it whole; elsewhere it draws a new one
    if (this.touch) this.drag = x > a - 20 && x < b + 20 ? "move" : "new"; else this.drag = Math.abs(x - a) < 6 ? "start" : Math.abs(x - b) < 6 ? "end" : x > a && x < b ? "move" : "new";
    this.anchor = { x, start: this.start, end: this.end, day: this.day(x) };
  }
  move(e) {
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, e.offsetX);
    if (this.pinch && this.pointers.size === 2) {  // the span shrinks or grows with the fingers' gap
      const [x1, x2] = [...this.pointers.values()], { gap, t, view } = this.pinch;
      this.zoomTo(t, (view[1] - view[0]) * gap / (Math.abs(x2 - x1) || 1), (t - view[0]) / (view[1] - view[0]));
      return;
    }
    this.hover(e.offsetX);
    if (!this.drag) {
      const [a, b] = [this.x(this.start), this.x(this.end)];
      this.canvas.style.cursor = Math.abs(e.offsetX - a) < 6 || Math.abs(e.offsetX - b) < 6 ? "ew-resize" :
        e.offsetX > a && e.offsetX < b ? "grab" : "crosshair";
      return;
    }
    const max = info.max_days * DAY, t = this.day(e.offsetX), [lo, hi] = this.full;
    const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
    if (this.drag === "start") this.start = clamp(t, this.end - max, this.end - DAY);  // the dragged edge stops at the limit
    else if (this.drag === "end") this.end = clamp(t, this.start + DAY, this.start + max);
    else if (this.drag === "move") {
      const len = this.anchor.end - this.anchor.start;
      this.start = clamp(this.anchor.start + t - this.anchor.day, lo, hi - len);
      this.end = this.start + len;
    } else {  // a new band from where the drag began, extended toward the mouse
      const a = this.anchor.day;
      [this.start, this.end] = t >= a ? [a, clamp(t + DAY, a + DAY, a + max)] : [clamp(t, a + DAY - max, a), a + DAY];
    }
    this.draw();
  }
  up(e) {
    this.pointers.delete(e.pointerId);
    if (this.pinch) {
      if (!this.pointers.size) this.pinch = null;
      return;
    }
    if (!this.drag) return;
    this.drag = null;
    this.onChange(this.start, Math.round((this.end - this.start) / DAY));
  }
  hover(x) {
    const d = this.ticks.day, t = this.t(x);
    let i = d.findIndex((v) => v + DAY / 2 >= t);
    if (i > 0 && Math.abs(d[i - 1] + DAY / 2 - t) < Math.abs(d[i] + DAY / 2 - t)) i -= 1;
    const day = Math.floor(t / DAY) * DAY;
    $("#readout").textContent = i >= 0 && d[i] === day
      ? `${dayText(day)} · cycle ${this.ticks.cycle[i]} · ${this.ticks.n[i]} passage${this.ticks.n[i] > 1 ? "s" : ""} over the region`
      : `${dayText(day)} · no passage over the region`;
  }
  zoom(e) {
    e.preventDefault();
    const t = this.t(e.offsetX), span = (this.view[1] - this.view[0]) * (e.deltaY > 0 ? 1.25 : 0.8);
    this.zoomTo(t, span, (t - this.view[0]) / (this.view[1] - this.view[0]));
  }
  zoomTo(t, span, at) {  // a view `span` long, with time t at the fraction `at` of the width
    const [lo, hi] = this.full;
    span = Math.min(Math.max(span, 20 * DAY), hi - lo);
    const a = Math.min(Math.max(t - at * span, lo), hi - span);
    this.view = [a, a + span];
    this.draw();
  }
}

// --- overview: Equal Earth world with the map's view, bottom left
const overview = { image: new Image() };
function drawOverview() {
  const c = $("#overview"), ctx = c.getContext("2d"), dpr = devicePixelRatio || 1, [W, H] = [170, 83];
  [c.width, c.height] = [W * dpr, H * dpr];
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  if (document.body.classList.contains("globe")) return;
  if (overview.image.complete) ctx.drawImage(overview.image, 0, 0, W, H);
  const [X, Y] = info.world, A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
  const ee = (lon, lat) => {  // Equal Earth (Šavrič et al. 2018) on the unit sphere, to pixels
    const l = lon * Math.PI / 180, t = Math.asin(M * Math.sin(lat * Math.PI / 180)), t2 = t * t, t6 = t2 * t2 * t2;
    const x = 2 * Math.sqrt(3) * l * Math.cos(t) / (3 * (9 * A4 * t6 * t2 + 7 * A3 * t6 + 3 * A2 * t2 + A1));
    const y = t * (A4 * t6 * t2 + A3 * t6 + A2 * t2 + A1);
    return [(x + X) / (2 * X) * W, (Y - y) / (2 * Y) * H];
  };
  const b = map.getBounds(), clamp = (v, a) => Math.max(-a, Math.min(a, v));
  let [w, e] = [b.getWest(), b.getEast()];
  if (e - w >= 300) return;  // the map shows (nearly) the whole world: nothing to point at
  else { const c0 = wrap((w + e) / 2); [w, e] = [clamp(c0 - (e - w) / 2, 180), clamp(c0 + (e - w) / 2, 180)]; }
  const [s, n] = [clamp(b.getSouth(), 89), clamp(b.getNorth(), 89)];
  ctx.beginPath();
  const edge = (f) => { for (let i = 0; i <= 24; i++) ctx.lineTo(...ee(...f(i / 24))); };
  edge((t) => [w + t * (e - w), s]); edge((t) => [e, s + t * (n - s)]);
  edge((t) => [e - t * (e - w), n]); edge((t) => [w, n - t * (n - s)]);
  ctx.closePath();
  ctx.fillStyle = "rgba(249,115,22,0.25)";
  ctx.strokeStyle = "#f97316";
  ctx.lineWidth = 1.5;
  ctx.fill();
  ctx.stroke();
  const [cx, cy] = ee((w + e) / 2, (s + n) / 2);
  ctx.fillStyle = "#f97316";
  ctx.beginPath(); ctx.arc(cx, cy, 2.5, 0, 2 * Math.PI); ctx.fill();
}

function dragByHeader(card, moved = () => {}) {
  card.querySelector("header").addEventListener("pointerdown", (e) => {
    if (e.target.closest("select, button")) return;
    const [x0, y0, l0, t0] = [e.clientX, e.clientY, card.offsetLeft, card.offsetTop];
    const move = (m) => {
      Object.assign(card.style, { left: `${l0 + m.clientX - x0}px`, top: `${t0 + m.clientY - y0}px`, right: "auto", bottom: "auto" });
      moved();
    };
    const stop = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", stop); };
    addEventListener("pointermove", move);
    addEventListener("pointerup", stop);
  });
}

// --- the time series at a clicked point: a card tied to the point by a line (a sheet on a small screen)
let pin = null, pointCard = null;
function closePoint() {
  pin?.remove();
  pointCard?.tip?.remove();
  pointCard?.remove();
  pin = pointCard = null;
  updateSheets();
  linkSoon();
}

// on a small screen a card is a sheet in the color bar's place (hidden meanwhile), small; its grab bar (a tap,
// or a drag up or down) makes it tall or small again
function updateSheets() {
  document.body.classList.toggle("card-open", narrow() && !!(pointCard?.classList.contains("sheet") || transect));
}
function grab(card) {
  const bar = document.createElement("div");
  bar.className = "grab";
  card.prepend(bar);
  // a drag decides on release; a tap on its click, which then lands on the bar (resizing on release would send
  // the tap's click to whatever is under the finger in the resized card: the chart, which changes the dates)
  let y0 = null, dragged = false;
  bar.addEventListener("pointerdown", (e) => { y0 = e.clientY; dragged = false; bar.setPointerCapture(e.pointerId); });
  bar.addEventListener("pointerup", (e) => {
    const dy = e.clientY - y0;
    if ((dragged = Math.abs(dy) >= 10)) card.classList.toggle("tall", dy < 0);
  });
  bar.addEventListener("click", () => { if (!dragged) card.classList.toggle("tall"); });
}
async function openPoint(lngLat) {  // a small card: where, and the value there; the time series on request
  closePoint();
  const sheet = narrow();
  const p = map.project(lngLat), size = map.getContainer().getBoundingClientRect();
  const right = p.x < size.width * 0.55, below = p.y < size.height * 0.45;
  const el = document.createElement("div");
  el.className = "pin";
  el.innerHTML = `<svg><line x1="0" y1="0"/></svg>
    <div class="dot"></div>
    <div class="card compact">
      <header><div><h3>${latText(lngLat.lat)} ${lonText(lngLat.lng)}</h3>
        <p class="hint"><span class="spinner"></span></p></div>
        <div class="tools"><span class="chart-buttons hidden">${chartButtons()}</span>
          <button class="ghost close" title="Close"><i data-lucide="x"></i></button></div></header>
      <button class="expand" title="Mean around this point, every passage of the mission"><i data-lucide="chart-line"></i>Time series</button>
      <div class="chart"></div></div>`;
  const card = pointCard = el.querySelector(".card");
  card.lngLat = lngLat;
  linkSoon();
  track("point", { point_lon: +lngLat.lng.toFixed(4), point_lat: +lngLat.lat.toFixed(4) });
  wireChart(card);
  if (sheet) {
    card.classList.add("sheet");
    document.body.appendChild(card);
    el.querySelector("svg").remove();
    grab(card);
  }
  for (const ev of ["mousedown", "mouseup", "click", "dblclick", "wheel", "pointerdown"]) card.addEventListener(ev, (e) => e.stopPropagation());
  card.querySelector(".close").onclick = closePoint;
  // the line goes from the point to the card's nearest corner; the card can be moved (by its header) and resized
  const line = el.querySelector("line");
  const link = () => {
    if (sheet) return;
    const [l, t, w, h] = [card.offsetLeft, card.offsetTop, card.offsetWidth, card.offsetHeight];
    line.setAttribute("x2", Math.min(Math.max(0, l), l + w));
    line.setAttribute("y2", Math.min(Math.max(0, t), t + h));
  };
  const place = (w, h, first = false) => {  // on the side with room; when it grows, the corner near the point stays
    if (sheet) return;
    const x = first ? (right ? 40 : -40) : card.offsetLeft + (right ? 0 : card.offsetWidth);
    const y = first ? (below ? 40 : -40) : card.offsetTop + (below ? 0 : card.offsetHeight);
    card.style.left = `${right ? x : x - w}px`;
    card.style.top = `${below ? y : y - h}px`;
  };
  if (!sheet) dragByHeader(card, link);
  card.querySelector(".expand").onclick = () => {
    const [w, h] = [440, 250];
    place(w, h);
    card.classList.remove("compact");
    if (!sheet) Object.assign(card.style, { width: `${w}px`, height: `${h}px` });
    card.querySelector("h3").textContent += ` · ${info.variables[state.variable]}`;
    card.querySelector(".chart-buttons").classList.remove("hidden");
    fillSeries(card, lngLat, RADIUS);
    linkSoon();
    track("series", { point_lon: +lngLat.lng.toFixed(4), point_lat: +lngLat.lat.toFixed(4) });
  };
  new ResizeObserver(() => {
    link();
    card.plot?.setSize(chartSize(card));
  }).observe(card);
  pin = new maplibregl.Marker({ element: el, anchor: "top-left" }).setLngLat(lngLat).addTo(map);
  updateSheets();
  lucide.createIcons();
  place(card.offsetWidth, card.offsetHeight, true);
  link();
  const v = state.data ? await api(`/api/value?${state.view}`, { lon: wrap(lngLat.lng), lat: lngLat.lat, mode: state.mode })
    .catch(() => ({})) : {};
  const bottom = card.offsetTop + card.offsetHeight;  // the text may take more lines: keep the edge near the point
  const mean = v.passages?.length > 1 ? ` (mean of ${v.passages.length} passages)` : "";
  card.querySelector(".hint").textContent = v.value == null ? "No data here on the selected dates"
    : [`${v.value.toFixed(3)} ${unit()}${mean} · ${$("#legend-variable").textContent}`,
       ...v.passages.map((q) => `Pass ${q.pass_id} · cycle ${q.cycle} · ${timeText(q.time)}`)].join("\n");
  if (!below && !sheet) card.style.top = `${bottom - card.offsetHeight}px`;
  link();
}

function touchCursor(u) {  // a finger on a chart moves its cursor, as a mouse would
  for (const ev of ["touchstart", "touchmove"]) u.over.addEventListener(ev, (e) => {
    const r = u.over.getBoundingClientRect(), t = e.touches[0];
    u.setCursor({ left: t.clientX - r.left, top: t.clientY - r.top });
    e.preventDefault();
  }, { passive: false });
}

function placeTip(tip, u, x, y) {  // a tip on the page, by the point (x, y) of a chart, inside the window
  const r = u.over.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${Math.min(Math.max(r.left + x - w / 2, 8), innerWidth - w - 8)}px`;
  tip.style.top = `${Math.max(r.top + y - h - 12, 8)}px`;
}

function chartSize(card) {  // the chart fills the card below its header (and grab bar)
  return { width: card.clientWidth - 24, height: card.clientHeight - card.querySelector(".chart").offsetTop - 12 };
}

const RADIUS = 5, FINE_RADIUS = 1;  // km: the series' circles (2 km pixels: ~20; 250 m pixels: ~50)
const SERIES = [{ color: "#3b82f6" }, { color: "#f97316" }];  // 2 km, 250 m

async function fillSeries(card, lngLat, radius) {
  const chart = card.querySelector(".chart"), hint = card.querySelector(".hint");
  card.plot?.destroy();
  card.plot = null;
  card.tip?.remove();
  chart.innerHTML = `<div class="loading"><span class="spinner"></span>Reading the whole mission from the cloud…</div>`;
  hint.textContent = `mean within ${radius} km`;
  const where = { lon: wrap(lngLat.lng), lat: lngLat.lat, variable: state.variable };
  // the 250 m series (a smaller circle of the detail product) comes later, drawn over the 2 km one when it arrives
  const fine = info.looks[state.variable].fine ? api("/api/series", { ...where, radius: FINE_RADIUS, fine: true }).catch(() => null) : null;
  try {
    const s = await api("/api/series", { ...where, radius });
    if (card.classList.contains("sheet")) card.querySelector("h3").textContent = `${latText(lngLat.lat)} ${lonText(lngLat.lng)} · ${passages(s.time.length)}`;
    if (!s.time.length) { chart.innerHTML = `<div class="loading">No passage here (outside the swaths, or in the nadir gap).</div>`; return; }
    card.series = s;
    drawSeries(card, [s], radius);
    if (!fine) return;
    const f = await fine;
    if (!card.isConnected || card.series !== s) return;  // closed, or drawn again meanwhile
    if (card.plot && f?.time.length) drawSeries(card, [s, f], radius);
    else seriesLegend(card, [s], radius, f ? "no 250 m data here yet" : "250 m not available");
  } catch (e) {
    chart.innerHTML = `<div class="loading">${e.message}</div>`;
  }
}

function seriesLegend(card, curves, radius, fineNote = "reading…") {
  // which curve is which, each a toggle: "● 2 km pixels, within 5 km · 412 passages  ● 250 m pixels, within 1 km · ..."
  const hint = card.querySelector(".hint"), u = card.plot;
  const text = [`2 km pixels, within ${radius} km · ${passages(curves[0].time.length)}`,
                `250 m pixels, within ${FINE_RADIUS} km · ${curves[1] ? passages(curves[1].time.length) : fineNote}`];
  hint.innerHTML = (info.looks[state.variable].fine ? text : text.slice(0, 1)).map((t, k) =>
    `<span class="key${u && u.series[k + 1] && !u.series[k + 1].show ? " off" : ""}" data-k="${k}" title="Show or hide this series">` +
    `<i style="background:${SERIES[k].color}"></i>${t}</span>`).join("") + `<span class="key-note"> · click a point to see that date on the map</span>`;
  hint.querySelectorAll(".key").forEach((el) => el.onclick = (e) => {
    e.stopPropagation();
    const i = Number(el.dataset.k) + 1;
    if (!card.plot || !card.plot.series[i]) return;
    card.plot.setSeries(i, { show: !card.plot.series[i].show });
    el.classList.toggle("off", !card.plot.series[i].show);
  });
}

function drawSeries(card, curves, radius) {
  const chart = card.querySelector(".chart");
  card.plot?.destroy();
  card.tip?.remove();
  chart.innerHTML = "";
  // one time axis for both: the passages of either (the same passage is at the same time in both products)
  const xs = [...new Set(curves.flatMap((c) => c.time))].sort((a, b) => a - b);
  const ys = curves.map((c) => { const at = new Map(c.time.map((t, k) => [t, c.value[k]])); return xs.map((t) => at.get(t) ?? null); });
  const band = { hooks: { drawClear: [(u) => {  // the selected dates
    const [a, b] = [u.valToPos(state.start / 1000, "x", true), u.valToPos((state.start + state.days * DAY) / 1000, "x", true)];
    u.ctx.fillStyle = "rgba(249,115,22,0.25)";
    u.ctx.fillRect(a, u.bbox.top, Math.max(b - a, 2), u.bbox.height);
  }] } };
  const axis = { stroke: "#94a3b8", grid: { stroke: "rgba(148,163,184,0.12)" }, ticks: { stroke: "rgba(148,163,184,0.25)" } };
  const tip = document.createElement("div");  // date and values of the passage under the mouse, on the page
  tip.className = "tip floating";
  document.body.appendChild(tip);
  card.tip = tip;
  const hover = { hooks: { init: [(u) => {
    touchCursor(u);
    u.over.addEventListener("click", () => {  // that passage's date on the map, keeping the number of days
      if (u.cursor.idx == null) return;
      const day = Math.floor(u.data[0][u.cursor.idx] * 1000 / DAY) * DAY;
      const start = Math.min(Math.max(day - Math.floor((state.days - 1) / 2) * DAY, timeline.full[0]),
                             timeline.full[1] - state.days * DAY);
      setDates(start, state.days);
    });
  }], setCursor: [(u) => {
    const i = u.cursor.idx, k = i == null ? -1 : u.data.slice(1).findIndex((y, j) => y[i] != null && u.series[j + 1].show);
    tip.style.display = k < 0 ? "none" : "block";
    if (k < 0) return;
    const values = u.data.slice(1).map((y, j) => y[i] == null || !u.series[j + 1].show ? ""
      : `${curves.length > 1 ? (j ? "250 m " : "2 km ") : ""}${y[i].toFixed(3)} ${unit()}`).filter(Boolean);
    tip.textContent = `${dayText(u.data[0][i] * 1000)} · ${values.join(" · ")}`;
    placeTip(tip, u, u.valToPos(u.data[0][i], "x"), u.valToPos(u.data[k + 1][i], "y"));
  }] } };
  card.plot = new uPlot({
    ...chartSize(card), plugins: [band, hover], legend: { show: false }, cursor: { points: { size: 7 } },
    scales: { x: { time: true } },
    axes: [axis, { ...axis, label: unit(), size: 45 }],
    series: [{}, ...curves.map((_, k) => ({ stroke: SERIES[k].color, width: 1, spanGaps: true,
                                           points: { show: true, size: 4, fill: SERIES[k].color } }))],
  }, [xs.map((t) => t / 1000), ...ys], chart);
  seriesLegend(card, curves, radius);
}

// --- mouse position (and value) in the lower right corner
function setupCoords() {
  // the value under the mouse: once it rests 150 ms, one request at a time (a newer one cancels it), none while the
  // map moves or a button is held (each reads passages: dozens at once held the tiles back); zoomed out, the daily
  // rasters' value, nothing read
  let timer = null, asking = null;
  map.on("mousemove", (e) => {
    const where = `${latText(e.lngLat.lat, 3)}  ${lonText(e.lngLat.lng, 3)}`;
    $("#coords").textContent = where;
    clearTimeout(timer);
    if (!state.data || map.isMoving() || e.originalEvent.buttons) return;
    timer = setTimeout(async () => {
      asking?.abort();
      const ctl = (asking = new AbortController()), t0 = performance.now();
      const q = new URLSearchParams({ lon: wrap(e.lngLat.lng), lat: e.lngLat.lat, mode: state.mode, quick: map.getZoom() < 6 ? 1 : 0 });
      const v = await fetch(`/api/value?${state.view}&${q}`, { signal: ctl.signal }).then((r) => r.json()).catch(() => ({}));
      dbg({ ev: "value", ms: Math.round(performance.now() - t0), quick: q.get("quick") === "1", aborted: ctl.signal.aborted });
      if (asking === ctl) asking = null;
      if (v.value != null && $("#coords").textContent.startsWith(where)) $("#coords").textContent = `${where} · ${v.value.toFixed(3)} ${unit()}`;
    }, 150);
  });
  map.getCanvas().addEventListener("mouseleave", () => { $("#coords").textContent = ""; });
}

// --- a line drawn on the map: the values along it, one curve per passage of the selected dates
const PALETTE = ["#3b82f6", "#f97316", "#22c55e", "#e879f9", "#facc15", "#14b8a6", "#f43f5e", "#a78bfa"];
let transect = null;  // { points, step, card, key }

function drawLine() {  // the next clicks on the map are its vertices; Done (or a double click) ends it
  if (state.drawing) drawBox();
  state.lining = !state.lining;
  state.vertices = [];
  map.getCanvas().style.cursor = state.lining ? "crosshair" : "";
  state.lining ? map.doubleClickZoom.disable() : map.doubleClickZoom.enable();
  if (!state.lining) showLine(transect?.path ?? transect?.points ?? []);
  showDrawing();
}

function endLine() {  // Done, or a double click: the values along the line drawn so far
  const points = state.vertices;
  drawLine();
  if (points.length > 1) openTransect(points);
}

function showLine(points, mouse) {  // on the map, with the segment to the mouse while drawing
  const c = mouse ? [...points, mouse] : points;
  map.getSource("line").setData({ type: "Feature", geometry: c.length > 1 ? { type: "LineString", coordinates: c }
                                                                        : { type: "Point", coordinates: [0, 0] } });
}

function showProbe(lngLat) {  // the place on the line under the chart's cursor
  map.getSource("probe").setData({ type: "FeatureCollection", features: lngLat ? [{ type: "Feature",
    geometry: { type: "Point", coordinates: lngLat } }] : [] });
}

function setupLine() {
  map.on("click", (e) => {
    if (!state.lining) return;
    const last = state.vertices.at(-1), p = map.project(e.lngLat);
    if (last && Math.hypot(p.x - map.project(last).x, p.y - map.project(last).y) < 4) return;  // the double click's
    // each vertex the short way from the previous one (segments are great circles), so the line drawn so far
    // does not go around the world when it crosses 180
    state.vertices.push([last ? near(e.lngLat.lng, last[0]) : e.lngLat.lng, e.lngLat.lat]);
    showLine(state.vertices);
    showDrawing();
  });
  map.on("mousemove", (e) => {
    const last = state.lining && state.vertices.at(-1);
    if (last) showLine(state.vertices, [near(e.lngLat.lng, last[0]), e.lngLat.lat]);
  });
  map.on("dblclick", (e) => {
    if (!state.lining) return;
    e.preventDefault();
    endLine();
  });
  $("#done").onclick = endLine;
  $("#undo").onclick = () => { state.vertices.pop(); showLine(state.vertices); showDrawing(); };
  $("#cancel").onclick = () => state.lining ? drawLine() : drawBox();
  addEventListener("keydown", (e) => { if (e.key === "Escape") { if (state.lining) drawLine(); else if (state.drawing) drawBox(); } });
}

function openTransect(points) {
  transect?.card.tip?.remove();
  transect?.card.remove();
  const card = document.createElement("section");
  card.className = "card transect";
  card.innerHTML = `<header><div><h3>Along the line</h3><p class="hint"><span class="spinner"></span></p></div>
      <div class="tools"><div class="segmented kind"><button data-v="profile" class="active"
          title="The values along the line on the selected dates">Profile</button><button data-v="hovmoller"
          title="Distance and time: every passage of the mission along the line">Hovmöller</button></div>
        <select class="hov-mode hidden" title="What is subtracted from the values">
          <option value="time">minus time mean</option><option value="none">as stored</option></select>
        <span class="chart-buttons">${chartButtons()}</span>
        <select class="step${info.detail ? "" : " hidden"}" title="Resolution: ${info.product}, or ${info.detail?.product}">
          <option value="2" selected>2 km</option><option value="0.25">250 m</option></select>
        <button class="ghost close" title="Close"><i data-lucide="x"></i></button></div></header>
    <div class="chart"></div>`;
  document.body.appendChild(card);
  if (narrow()) grab(card);
  wireChart(card);
  lucide.createIcons();
  transect = { points, step: 2, card, kind: "profile", hovMode: "time" };
  linkSoon();
  if (state.area && isGlobe(state.box) && !isGlobe(state.area) && !areaHolds(state.area, areaWanted())) loadData();  // the line left the area read
  updateSheets();
  card.querySelector(".close").onclick = () => {
    card.tip?.remove(); card.remove(); transect = null; showLine([]); showProbe(null); updateSheets(); linkSoon();
  };
  card.querySelector(".step").onchange = (e) => { transect.step = Number(e.target.value); fillLine(); linkSoon(); };
  card.querySelector(".hov-mode").onchange = (e) => { transect.hovMode = e.target.value; fillHovmoller(); linkSoon(); };
  card.querySelector(".kind").onclick = (e) => {
    if (!e.target.dataset.v || e.target.dataset.v === transect.kind) return;
    transect.kind = e.target.dataset.v;
    linkSoon();
    card.querySelectorAll(".kind button").forEach((b) => b.classList.toggle("active", b === e.target));
    card.querySelector(".hov-mode").classList.toggle("hidden", transect.kind !== "hovmoller");
    // time against distance needs room: a taller card (a tall sheet on a small screen)
    if (transect.kind === "hovmoller") narrow() ? card.classList.add("tall") : (card.style.height = `${Math.max(card.offsetHeight, 520)}px`);
    else card.querySelector(".chart").title = "";
    fillLine();
  };
  dragByHeader(card);
  new ResizeObserver(() => card.plot?.setSize(transectSize(card))).observe(card);
  showLine(points);
  fillLine();
}

const transectSize = chartSize;
// what the line's card shows depends on: the profile on the view (box, dates, variable) and the mode; the
// Hovmöller (the whole mission) on the variable and the mode only
const lineKey = () => transect.kind === "profile" ? `${state.view}|${state.mode}|${transect.step}`
  : `hovmoller|${state.variable}|${transect.step}`;
// a line with no passage: at 250 m, maybe only because L2 Unsmoothed does not have those dates yet (it is still being
// uploaded): what it covers so far, and the line at 2 km
function noPassage(text) {
  if (transect.step >= 1 || !info.detail) return `<div class="loading">${text}</div>`;
  const covers = `${dayText(Date.parse(info.detail.first + "Z"))} to ${dayText(Date.parse(info.detail.last + "Z"))}`;
  setTimeout(() => {
    const b = transect?.card.querySelector(".at-2km");
    if (b) b.onclick = () => { transect.step = 2; transect.card.querySelector(".step").value = "2"; fillLine(); linkSoon(); };
  });
  return `<div class="loading">${info.detail.product} has no passage here on these dates yet: so far it covers
    ${covers}.<br><button class="ghost at-2km">Show at 2 km</button></div>`;
}

const fillLine = () => {  // a variable without 250 m (SWH, wind): the line at 2 km, and no 250 m to choose
  const select = transect.card.querySelector(".step"), fine = info.looks[state.variable].fine;
  select.querySelector('option[value="0.25"]').disabled = !fine;
  if (!fine && transect.step < 1) { transect.step = 2; select.value = "2"; }
  return (transect.kind === "profile" ? fillTransect : fillHovmoller)();
};

function setPath(t, points) {
  // the line through the points read (great circles), with longitudes continuous from the first vertex,
  // so it and the marker are on the same copy of the world as the drawing
  let prev = points[0][0];
  transect.path = t.lon.map((x, i) => { prev = x + 360 * Math.round((prev - x) / 360); return [prev, t.lat[i]]; });
  showLine(transect.path);
}

const distanceText = (km) => km < 1 ? `${Math.round(km * 1000)} m` : `${+km.toFixed(2)} km`;

async function fillTransect() {
  const { card, points, step } = transect, chart = card.querySelector(".chart"), hint = card.querySelector(".hint");
  const key = transect.key = lineKey();
  card.plot?.destroy();
  card.plot = null;
  card.tip?.remove();
  chart.innerHTML = `<div class="loading"><span class="spinner"></span>Reading the values along the line${step < 1 ? " at 250 m" : ""}…</div>`;
  try {
    const t = await api(`/api/transect?${state.view}`, { mode: state.mode, step,
      points: points.map(([x, y]) => `${wrap(x).toFixed(4)},${y.toFixed(4)}`).join(";") });
    // asked again meanwhile (another resolution, view or kind; or closed): an older answer must not win
    if (transect?.card !== card || transect.kind !== "profile" || transect.key !== key) return;
    setPath(t, points);
    transect.km = Math.round(t.distance.at(-1));
    track("line", lineFields());
    card.querySelector("h3").textContent = `Along the line · ${t.distance.at(-1).toFixed(0)} km · ${$("#legend-variable").textContent}`;
    hint.textContent = `${passages(t.passages.length)} · nearest pixel of each, every ${distanceText(t.step)}`;
    if (narrow()) card.querySelector("h3").textContent = `Line · ${t.distance.at(-1).toFixed(0)} km · ${passages(t.passages.length)}`;
    chart.innerHTML = "";
    if (!t.passages.length) {
      chart.innerHTML = noPassage("No passage crosses the line on the selected dates.");
      return;
    }
    const color = (k) => PALETTE[k % PALETTE.length];
    const axis = { stroke: "#94a3b8", grid: { stroke: "rgba(148,163,184,0.12)" }, ticks: { stroke: "rgba(148,163,184,0.25)" } };
    const tip = document.createElement("div");  // on the page, not in the card: it may stick out of it
    tip.className = "tip floating";
    document.body.appendChild(tip);
    card.tip = tip;
    const hover = { hooks: { init: [touchCursor], setCursor: [(u) => {
      const i = u.cursor.idx;
      tip.style.display = i == null ? "none" : "block";
      showProbe(i == null ? null : transect.path[i]);
      if (i == null) return;
      // a table: the column names in orange, then one row per passage with data here
      const here = t.passages.map((q, k) => ({ q, k, v: q.value[i], at: new Date(q.line_time[i]) })).filter((h) => h.v != null);
      // the curve under the mouse (within 20 px) stands out in the table: colors repeat, rows don't
      const near = here.map((h) => Math.abs(u.valToPos(h.v, "y") - u.cursor.top));
      const on = near.length && Math.min(...near) < 20 ? near.indexOf(Math.min(...near)) : -1;
      const date = (d) => d.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" });
      tip.innerHTML = `<b>${t.distance[i].toFixed(0)} km</b>` + (here.length ? `<table class="${on < 0 ? "" : "picked"}">
        <tr><th></th><th>value</th><th>dd/mm/yyyy</th><th>hh:mm UTC</th><th>pass</th><th>cycle</th></tr>` +
        here.map((h, n) => `<tr class="${n === on ? "on" : ""}"><td><i style="background:${color(h.k)}"></i></td><td>${h.v.toFixed(3)} ${unit()}</td>
          <td>${date(h.at)}</td><td>${h.at.toISOString().slice(11, 16)}</td><td>${h.q.pass_id}</td><td>${h.q.cycle}</td></tr>`).join("") +
        "</table>" : "<div>no data</div>");
      const r = u.over.getBoundingClientRect(), x = r.left + u.valToPos(t.distance[i], "x");  // right of the cursor, if it fits
      tip.style.left = `${x + 12 + tip.offsetWidth < innerWidth - 8 ? x + 12 : x - 12 - tip.offsetWidth}px`;
      tip.style.top = `${r.top + 4}px`;
    }] } };
    card.plot = new uPlot({
      ...transectSize(card), plugins: [hover], legend: { show: false }, cursor: { points: { size: 6 } },
      scales: { x: { time: false } },
      axes: [{ ...axis, label: "km", labelSize: 16 }, { ...axis, label: unit(), size: 45 }],
      series: [{}, ...t.passages.map((q, k) => ({ stroke: color(k), width: 1.5 }))],
    }, [t.distance, ...t.passages.map((q) => q.value)], chart);
  } catch (e) {
    chart.innerHTML = `<div class="loading">${e.message}</div>`;
  }
}

// --- the Hovmöller of a line: distance and time, every passage of the mission at its own time. Like a pcolor
// with holes: each passage's cell reaches halfway to the next passage at that point, at most 4 px, so the whole
// mission looks filled and zooming in shows the passages apart.
function lut(colors) {  // the map's colors, 256 steps, as pixels (RGBA in a little-endian uint32)
  const rgb = colors.map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
  return Uint32Array.from({ length: 256 }, (_, k) => {
    const f = k / 255 * (rgb.length - 1), i = Math.min(Math.floor(f), rgb.length - 2), w = f - i;
    const [r, g, b] = rgb[i].map((c, j) => Math.round(c + (rgb[i + 1][j] - c) * w));
    return ((255 << 24) | (b << 16) | (g << 8) | r) >>> 0;
  });
}

async function fillHovmoller() {
  const { card, points, step } = transect, chart = card.querySelector(".chart"), hint = card.querySelector(".hint");
  transect.key = lineKey();
  card.plot?.destroy();
  card.plot = null;
  card.tip?.remove();
  chart.innerHTML = `<div class="loading"><span class="spinner"></span>Reading the whole mission along the line${step < 1
    ? " at 250 m… (only the passages L2 Unsmoothed has so far)" : "… (a line across an ocean can take some 10-20 s)"}</div>`;
  try {
    // the values as stored, kept: changing what is subtracted does not read them again
    const key = transect.key;
    const h = transect.hov?.key === key ? transect.hov.h : await api("/api/hovmoller", { variable: state.variable, step,
      points: points.map(([x, y]) => `${wrap(x).toFixed(4)},${y.toFixed(4)}`).join(";") });
    if (transect?.card !== card || transect.kind !== "hovmoller") return;  // closed or switched meanwhile
    transect.hov = { key, h };
    setPath(h, points);
    transect.km = Math.round(h.distance.at(-1));
    track("line", lineFields());
    const km = h.distance.at(-1).toFixed(0), n = h.passages.length;
    card.querySelector("h3").textContent = narrow() ? `Hovmöller · ${km} km · ${passages(n)}`
      : `Along the line · ${km} km · whole mission · ${info.variables[state.variable]}`;
    chart.innerHTML = "";
    if (!n) { chart.innerHTML = noPassage("No passage crosses the line."); return; }
    // one column per point: the passages there in time order, minus (by default) the mean over time at that
    // point, so what stays (the mean topography of the surface) leaves and what changes shows
    const mode = transect.hovMode, avg = (v) => v.reduce((a, b) => a + b, 0) / v.length;
    const cols = h.distance.map(() => []);
    for (const q of h.passages) q.value.forEach((v, k) => { if (v != null) cols[q.first + k].push({ t: q.time / 1000, v, q }); });
    // a point where few passages have data (a swath's edge, a coast) has no mean over time worth the name:
    // it stays empty rather than a column of its own color
    const typical = [...cols.map((c) => c.length)].sort((a, b) => a - b)[Math.floor(cols.length / 2)];
    cols.forEach((c) => {
      c.sort((a, b) => a.t - b.t);
      if (mode !== "time") return;
      if (c.length < Math.max(8, typical / 4)) c.length = 0;
      else { const m = avg(c.map((e) => e.v)); c.forEach((e) => { e.v -= m; }); }
    });
    const all = cols.flat().map((c) => c.v).sort((a, b) => a - b);
    // colors at the 2-98 % percentiles, symmetric about zero unless the values are as stored
    let [lo, hi] = [all[Math.floor(all.length * 0.02)], all[Math.floor(all.length * 0.98)]];
    if (mode !== "none") hi = -(lo = -Math.max(-lo, hi));
    if (!(hi > lo)) [lo, hi] = [lo - 0.1, lo + 0.1];
    hint.textContent = `${passages(n)} · every ${distanceText(h.step)} · ${mode === "time" ? "minus the mean over time at each point" : "as stored"} · colors ${mode === "none" ? `${lo.toFixed(2)} to ${hi.toFixed(2)}` : `±${hi.toFixed(2)}`} ${unit()}`;
    chart.title = "Drag a box to zoom (the wheel zooms the time), double-click to go back, click a passage to see its date on the map";
    const [t0, t1] = [timeline.full[0] / 1000, timeline.full[1] / 1000], [d0, d1] = [h.distance[0], h.distance.at(-1)];
    const column = (u) => {  // the point under the cursor
      const d = u.posToVal(u.cursor.left, "x");
      return Math.min(Math.max(Math.round(d / h.step), 0), h.distance.length - 1);
    };
    const pick = (u) => {  // the cell under the cursor (within 8 px of its passage's time)
      if (u.cursor.left == null || u.cursor.left < 0) return null;
      const c = cols[column(u)], px = (e) => Math.abs(u.valToPos(e.t, "y") - u.cursor.top);
      const e = c.length ? c.reduce((a, b) => (px(b) < px(a) ? b : a)) : null;
      return e && px(e) < 8 ? e : null;
    };
    const cells = { hooks: { draw: [(u) => {
      // painted pixel by pixel into an image, then drawn at once: a line across an ocean is a million cells
      const ctx = u.ctx, dpr = devicePixelRatio || 1, cap = 4 * dpr, min = 0.5 * dpr, { left, top, width, height } = u.bbox;
      const colors = lut(stops());
      const [W, H] = [Math.round(width), Math.round(height)], img = new ImageData(W, H), px = new Uint32Array(img.data.buffer);
      const w = Math.abs(u.valToPos(h.step, "x", true) - u.valToPos(0, "x", true));
      cols.forEach((c, i) => {
        if (!c.length) return;
        const xc = u.valToPos(h.distance[i], "x", true) - left;
        const x0 = Math.max(Math.floor(xc - w / 2), 0), x1 = Math.min(Math.max(Math.ceil(xc + w / 2), x0 + 1), W);
        if (x1 <= 0 || x0 >= W) return;
        const y = c.map((e) => u.valToPos(e.t, "y", true) - top);  // later is higher up: y decreases
        c.forEach((e, k) => {
          const up = Math.max(k < c.length - 1 ? Math.min((y[k] - y[k + 1]) / 2, cap) : cap, min);
          const down = Math.max(k > 0 ? Math.min((y[k - 1] - y[k]) / 2, cap) : cap, min);
          const y0 = Math.max(Math.floor(y[k] - up), 0), y1 = Math.min(Math.ceil(y[k] + down), H);
          const color = colors[Math.round(Math.min(Math.max((e.v - lo) / (hi - lo), 0), 1) * 255)];
          for (let yy = y0; yy < y1; yy++) px.fill(color, yy * W + x0, yy * W + x1);
        });
      });
      const sheet = document.createElement("canvas");  // through a canvas, so the grid shows where there is no data
      [sheet.width, sheet.height] = [W, H];
      sheet.getContext("2d").putImageData(img, 0, 0);
      ctx.save();
      ctx.drawImage(sheet, left, top);
      // the selected dates: two orange lines
      ctx.fillStyle = "#f97316";
      for (const t of [state.start, state.start + state.days * DAY]) ctx.fillRect(left, u.valToPos(t / 1000, "y", true) - dpr / 2, width, dpr);
      ctx.restore();
    }] } };
    const tip = document.createElement("div");  // on the page, not in the card
    tip.className = "tip floating";
    document.body.appendChild(tip);
    card.tip = tip;
    const date = (d) => d.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" });
    const hover = { hooks: {
      init: [(u) => {
        touchCursor(u);
        let down = null;
        u.over.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
        u.over.addEventListener("click", (e) => {  // a click, not the end of a zoom box: that passage's date
          const e2 = pick(u);
          if (!e2 || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
          const day = Math.floor(e2.t * 1000 / DAY) * DAY;
          setDates(Math.min(Math.max(day - Math.floor((state.days - 1) / 2) * DAY, timeline.full[0]),
                            timeline.full[1] - state.days * DAY), state.days);
        });
        u.over.addEventListener("wheel", (e) => {  // the wheel zooms the time around the cursor
          e.preventDefault();
          const t = u.posToVal(e.offsetY, "y"), { min, max } = u.scales.y, f = e.deltaY > 0 ? 1.25 : 0.8;
          u.setScale("y", { min: Math.max(t - (t - min) * f, t0), max: Math.min(t + (max - t) * f, t1) });
        }, { passive: false });
      }],
      setCursor: [(u) => {
        const e = pick(u);
        showProbe(u.cursor.left == null || u.cursor.left < 0 ? null : transect.path[column(u)]);
        u.over.style.cursor = e ? "pointer" : "crosshair";
        tip.style.display = e ? "block" : "none";
        if (!e) return;
        const at = new Date(e.t * 1000);
        tip.innerHTML = `<b>${h.distance[column(u)].toFixed(0)} km</b><table><tr><th>value</th><th>dd/mm/yyyy</th>
          <th>hh:mm UTC</th><th>pass</th><th>cycle</th></tr><tr><td>${e.v.toFixed(3)} ${unit()}</td><td>${date(at)}</td>
          <td>${at.toISOString().slice(11, 16)}</td><td>${e.q.pass_id}</td><td>${e.q.cycle}</td></tr></table>`;
        placeTip(tip, u, u.cursor.left, u.cursor.top);
      }],
    } };
    const axis = { stroke: "#94a3b8", grid: { stroke: "rgba(148,163,184,0.12)" }, ticks: { stroke: "rgba(148,163,184,0.25)" } };
    card.plot = new uPlot({
      ...transectSize(card), plugins: [cells, hover], legend: { show: false },
      cursor: { points: { show: false }, drag: { x: true, y: true, uni: 8 },
                bind: { dblclick: (u) => () => { u.setScale("x", { min: d0, max: d1 }); u.setScale("y", { min: t0, max: t1 }); return null; } } },
      scales: { x: { time: false, auto: false, range: (u, a, b) => (a == null || isNaN(a) ? [d0, d1] : [a, b]) },
                y: { time: true, auto: false, range: (u, a, b) => (a == null || isNaN(a) ? [t0, t1] : [a, b]) } },
      axes: [{ ...axis, label: "km", labelSize: 16 }, { ...axis, size: 70 }],
      series: [{}, { stroke: "transparent" }],
    }, [h.distance, h.distance.map(() => null)], chart);
    card.plot.setScale("x", { min: d0, max: d1 });
    card.plot.setScale("y", { min: t0, max: t1 });
  } catch (e) {
    chart.innerHTML = `<div class="loading">${e.message}</div>`;
  }
}

// --- code in the dialog: the region's, or a chart's (with its figure)
function showCode(code, title, hint) {
  $("#code-title").textContent = title;
  $("#code-hint").innerHTML = hint;
  $("#code").textContent = code;
  Prism.highlightElement($("#code"));
  $("#copy").lastChild.textContent = "Copy";
  $("#code-dialog").showModal();
}
const showRegionCode = () => track("code", { what: "region" }) || showCode(state.code ?? "", "The same data in your own Python",
  "Only <code>pip install icechunk xarray</code>. This is the region and dates on the map.");

// --- a chart's buttons: the figure as PNG, its numbers as NetCDF, the code that reproduces it
const chartButtons = () => `<button class="ghost icon" data-get="png" title="Download the figure (PNG)"><i data-lucide="image-down"></i></button>` +
  (info.netcdf ? `<button class="ghost icon" data-get="nc" title="Download its data (NetCDF)"><i data-lucide="file-down"></i></button>` : "") +
  `<button class="ghost icon" data-get="code" title="The code that reproduces it (Python)"><i data-lucide="code"></i></button>`;

function chartQuery(card) {  // what the card shows, as the server's endpoints name it
  if (card === pointCard) {
    const [lon, lat, radius] = [wrap(card.lngLat.lng), card.lngLat.lat, RADIUS];
    return { kind: "series", params: { lon, lat, radius, variable: state.variable }, title: card.querySelector("h3").textContent };
  }
  const points = transect.points.map(([x, y]) => `${wrap(x).toFixed(4)},${y.toFixed(4)}`).join(";");
  const params = { points, step: transect.step, variable: state.variable };
  return { kind: transect.kind, params, title: card.querySelector("h3").textContent };
}

function wireChart(card) {  // on the card itself: the point's card stops clicks from going up
  card.addEventListener("click", (e) => {
    const b = e.target.closest("[data-get]");
    if (b) chartAction(card, b.dataset.get);
  });
}

async function chartAction(card, what) {
  const { kind, params, title } = chartQuery(card);
  track(what === "png" || what === "nc" ? "download" : "code", { what: `${kind}-${what}`, ...(kind === "series" ? {} : lineFields()) });
  if (what === "png") {  // the chart drawn large, under the card's title and subtitle, on the panel's color
    if (!card.plot) return;
    const size = { width: card.plot.width, height: card.plot.height };
    card.plot.setSize({ width: 1400, height: 600 });
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));  // uPlot draws on the next frame
    const plot = card.querySelector(".uplot canvas"), dpr = devicePixelRatio || 1;
    const pad = 64 * dpr, out = document.createElement("canvas");
    [out.width, out.height] = [plot.width, plot.height + pad];
    const ctx = out.getContext("2d");
    ctx.fillStyle = "#0f172a";
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.fillStyle = "#f9fafb";
    ctx.font = `600 ${14 * (devicePixelRatio || 1)}px Inter, sans-serif`;
    ctx.fillText(title, 20 * dpr, 28 * dpr);
    ctx.fillStyle = "#94a3b8";
    ctx.font = `${12 * dpr}px Inter, sans-serif`;
    const keys = [...card.querySelectorAll(".hint .key:not(.off)")];
    if (keys.length) {  // the series' keys, each with its color
      let x = 20 * dpr;
      for (const key of keys) {
        ctx.fillStyle = key.querySelector("i").style.background;
        ctx.fillRect(x, 41 * dpr, 9 * dpr, 9 * dpr);
        ctx.fillStyle = "#94a3b8";
        ctx.fillText(key.textContent, x + 14 * dpr, 50 * dpr);
        x += ctx.measureText(key.textContent).width + 34 * dpr;
      }
    } else ctx.fillText((card.querySelector(".hint")?.textContent ?? "").split(" · click")[0], 20 * dpr, 50 * dpr);  // no "click..."
    ctx.drawImage(plot, 0, pad);
    card.plot.setSize(size);
    const a = document.createElement("a");
    a.href = out.toDataURL("image/png");
    a.download = `swot_${kind}.png`;
    a.click();
  } else if (what === "nc") {
    const url = kind === "series" ? `/api/series.nc?${new URLSearchParams(params)}`
      : kind === "profile" ? `/api/transect.nc?${state.view}&${new URLSearchParams({ ...params, mode: state.mode })}`
      : `/api/hovmoller.nc?${new URLSearchParams(params)}`;
    const a = document.createElement("a");
    a.href = url;
    a.click();
  } else {
    const code = kind === "series" ? (await api("/api/code/series", params)).code
      : (await api("/api/code/line", kind === "profile" ? { ...params, start: isoDay(state.start), days: state.days }
                                                          : { ...params, mean: transect.hovMode })).code;
    showCode(code, "This chart in your own Python", `<code>pip install icechunk xarray matplotlib${kind === "series" ? "" : " scipy"}</code>.
      The same data and figure${kind === "hovmoller" ? " (the whole mission along the line: it reads a lot, give it a moment)" : ""}.`);
  }
}

// --- menus and dialogs
function setupMenus() {
  const menus = { "#region-button": "#region-menu", "#display-button": "#display-menu" };
  for (const [button, menu] of Object.entries(menus)) {
    $(button).onclick = (e) => {
      e.stopPropagation();
      for (const m of Object.values(menus)) if (m !== menu) $(m).classList.add("hidden");
      $(menu).classList.toggle("hidden");
      $(menu).style.top = `${$("#controls").offsetHeight + 20}px`;
    };
    $(menu).addEventListener("click", (e) => e.stopPropagation());
  }
  document.addEventListener("click", () => Object.values(menus).forEach((m) => $(m).classList.add("hidden")));
  $("#region-apply").onclick = () => {
    $("#region-menu").classList.add("hidden");
    setBox(Object.fromEntries(["west", "east", "south", "north"].map((k) => [k, Number($("#" + k).value)])));
  };
  $("#globe").onclick = () => { $("#region-menu").classList.add("hidden"); setBox({ west: -180, east: 180, south: -90, north: 90 }); };
  $("#draw").onclick = $("#tool-box").onclick = drawBox;
  $("#line").onclick = $("#tool-line").onclick = drawLine;
  $("#tool-menu").onclick = () => document.body.classList.toggle("sheet");
  $("#sheet-close").onclick = () => document.body.classList.remove("sheet");
  $("#tool-projection").onclick = () => setProjection(!document.body.classList.contains("globe"));
  $("#tool-code").onclick = showRegionCode;
  $("#tool-about").onclick = () => $("#about-dialog").showModal();
  $("#variable").onchange = () => {  // anomalies: a spatial mean subtracted, colors about zero; levels: as stored
    const was = isAnomaly();
    state.variable = $("#variable").value;
    if (isAnomaly() !== was) state.mode = isAnomaly() ? "box" : "none";
    state.symmetric = isAnomaly() && (state.variable !== "adt" || state.mode !== "none");
    loadData();
  };
  for (const [id, key] of [["#mode", "mode"], ["#preset", "preset"]]) {
    $(id).onclick = (e) => {
      if (!e.target.dataset.value) return;
      state[key] = e.target.dataset.value;
      if (key === "mode") state.symmetric = state.variable !== "adt" || state.mode !== "none";
      setColors();
    };
  }
  $("#symmetric").onchange = () => { state.symmetric = $("#symmetric").checked; setColors(); };
  for (const id of ["#vmin", "#vmax"]) $(id).onchange = () => {
    [state.preset, state.vmin, state.vmax] = ["custom", Number($("#vmin").value), Number($("#vmax").value)];
    setColors();
  };
  $("#basemap").innerHTML = Object.keys(BASEMAPS).map((k) => `<option>${k}</option>`).join("");
  $("#basemap").onchange = () => {
    const [url, brightness] = BASEMAPS[$("#basemap").value];
    map.getSource("basemap").setTiles([url]);
    map.setPaintProperty("basemap", "raster-brightness-max", brightness);
  };
  $("#code-button").onclick = showRegionCode;
  $("#about-button").onclick = () => $("#about-dialog").showModal();
  document.querySelectorAll("dialog .close").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
  $("#copy").onclick = async () => { await navigator.clipboard.writeText($("#code").textContent); $("#copy").lastChild.textContent = "Copied"; };

}

// --- globe or flat map (Web Mercator); the choice is remembered in this browser
function setProjection(globe) {
  // the same zoom shows the globe about a third as wide as the flat world: keep the world's size on screen
  const was = document.body.classList.contains("globe");
  if (was !== globe && map.getZoom() < 4) map.setZoom(map.getZoom() + (globe ? 0.7 : -0.7));
  map.setProjection({ type: globe ? "globe" : "mercator" });
  document.body.classList.toggle("globe", globe);  // the overview is for the flat map
  $("#projection").innerHTML = globe ? '<i data-lucide="map"></i>Flat map' : '<i data-lucide="earth"></i>Globe';
  $("#tool-projection").innerHTML = `<i data-lucide="${globe ? "map" : "earth"}"></i>`;
  lucide.createIcons();
  try { localStorage.setItem("projection", globe ? "globe" : "mercator"); } catch (e) {}
  drawOverview();
  linkSoon();
}

// --- anonymous usage (About says what): events, each with the view on screen; a session is a random number for
// this tab only (nothing is kept in the browser). Nothing is sent where the server keeps none (info.telemetry).
const SESSION = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
function track(event, extra = {}) {
  if (!info?.telemetry || !map || !state.box) return;
  const c = map.getCenter(), f = (x, n = 4) => +Number(x).toFixed(n);
  const body = JSON.stringify({ session: SESSION, event, screen: narrow() ? "phone" : "desktop", ...state.box,
    start: isoDay(state.start), days: state.days, variable: state.variable, lon: f(c.lng), lat: f(c.lat), zoom: f(map.getZoom(), 2),
    projection: document.body.classList.contains("globe") ? "globe" : "flat", mode: state.mode, cmap: cmapOf(), ...extra });
  if (!navigator.sendBeacon?.("/api/event", new Blob([body], { type: "application/json" })))
    fetch("/api/event", { method: "POST", body, keepalive: true, headers: { "Content-Type": "application/json" } }).catch(() => {});
}
function trackView(hash) {  // the view, once it has stayed a few seconds (not every step of a drag)
  clearTimeout(trackView.timer);
  trackView.timer = setTimeout(() => { if (hash !== trackView.last) { trackView.last = hash; track("view"); } }, 3000);
}
const lineFields = () => transect && { line: transect.points.map(([x, y]) => `${x.toFixed(3)},${y.toFixed(3)}`).join(";"),
  line_km: transect.km, line_kind: transect.kind, step: transect.step };

// --- the link: the address keeps what is on screen, after the # (kept up to date as people move, without a reload
// or history entries), and opening it rebuilds it all. view=W,E,S,N,start,days,variable & map=lon,lat,zoom,globe|flat
// & mode & cmap & range=mad|1|2|5|vmin,vmax & point=lon,lat[,series] & line=lon,lat;lon,lat/profile|hovmoller/step/hovMode
function linkNow() {
  if (!map || !state.box) return;
  const f = (x, n = 4) => +Number(x).toFixed(n), b = state.box, c = map.getCenter();
  const parts = [`view=${[b.west, b.east, b.south, b.north].map((x) => f(x, 3)).join(",")},${isoDay(state.start)},${state.days},${state.variable}`,
    `map=${f(c.lng)},${f(c.lat)},${f(map.getZoom(), 2)},${document.body.classList.contains("globe") ? "globe" : "flat"}`,
    `mode=${state.mode}`, `cmap=${cmapOf()}`, `range=${state.preset === "custom" ? `${state.vmin},${state.vmax}` : state.preset}`];
  if (pointCard?.lngLat) parts.push(`point=${f(pointCard.lngLat.lng)},${f(pointCard.lngLat.lat)}${pointCard.classList.contains("compact") ? "" : ",series"}`);
  if (transect) parts.push(`line=${transect.points.map(([x, y]) => `${f(x)},${f(y)}`).join(";")}/${transect.kind}/${transect.step}/${transect.hovMode}`);
  history.replaceState(null, "", "#" + parts.join("&"));
  trackView(parts.slice(0, 5).join("&"));  // the view (not the point or line: those are their own events)
}
function linkSoon() { clearTimeout(linkSoon.timer); linkSoon.timer = setTimeout(linkNow, 300); }

function readLink() {  // what the address after the # asks for (anything malformed is left out)
  const out = {};
  for (const part of location.hash.slice(1).split("&")) {
    const [key, value] = [part.slice(0, part.indexOf("=")), decodeURIComponent(part.slice(part.indexOf("=") + 1))];
    try {
      if (key === "view") {
        const v = value.split(",");
        const box = Object.fromEntries(["west", "east", "south", "north"].map((k, i) => [k, Number(v[i])]));
        if (Object.values(box).every(Number.isFinite) && info.variables[v[6]] && !isNaN(Date.parse(v[4])))
          out.view = { box, start: Date.parse(v[4] + "T00:00:00Z"), days: Math.min(Math.max(Number(v[5]) || 1, 1), info.max_days), variable: v[6] };
      } else if (key === "map") {
        const v = value.split(",");
        if ([0, 1, 2].every((i) => Number.isFinite(Number(v[i])))) out.map = { center: [Number(v[0]), Number(v[1])], zoom: Number(v[2]), globe: v[3] !== "flat" };
      } else if (key === "mode" && ["box", "passage", "none"].includes(value)) out.mode = value;
      else if (key === "cmap" && info.colormaps[value]) out.cmap = value;
      else if (key === "range") {
        const v = value.split(",").map(Number);
        if (v.length === 2 && v.every(Number.isFinite)) out.range = { vmin: v[0], vmax: v[1] };
        else if (["mad", "1", "2", "5"].includes(value)) out.range = { preset: value };
      } else if (key === "point") {
        const v = value.split(",");
        if (Number.isFinite(Number(v[0])) && Number.isFinite(Number(v[1]))) out.point = { lngLat: new maplibregl.LngLat(Number(v[0]), Number(v[1])), series: v[2] === "series" };
      } else if (key === "line") {
        const [pts, kind, step, hovMode] = value.split("/");
        const points = pts.split(";").map((xy) => xy.split(",").map(Number)).filter((xy) => xy.length === 2 && xy.every(Number.isFinite));
        if (points.length > 1) out.line = { points, kind: kind === "hovmoller" ? "hovmoller" : "profile", step: Number(step) === 0.25 ? 0.25 : 2,
                                            hovMode: hovMode === "none" ? "none" : "time" };
      }
    } catch (e) {}
  }
  return out;
}

function restoreLink(link) {  // the point and the line of a link, once its view is loaded
  if (link.point) {
    openPoint(link.point.lngLat);
    if (link.point.series) pointCard?.querySelector(".expand")?.click();
  }
  if (link.line) {
    openTransect(link.line.points);
    transect.hovMode = link.line.hovMode;
    transect.card.querySelector(".hov-mode").value = link.line.hovMode;
    if (link.line.step === 0.25 && info.looks[state.variable].fine) {
      transect.step = 0.25;
      transect.card.querySelector(".step").value = "0.25";
    }
    if (link.line.kind === "hovmoller") transect.card.querySelector('.kind [data-v="hovmoller"]').click();
    else fillLine();
  }
}

async function copyLink() {  // the public address (on the EC2: the link that wakes it) with the view
  linkNow();
  track("link");
  const url = (info.public_url || location.origin + location.pathname) + location.hash;
  try { await navigator.clipboard.writeText(url); toast("Link copied: it opens this view"); }
  catch (e) { prompt("Copy this link:", url); }
}

// --- start
async function main() {
  info = await api("/api/info");
  const link = readLink();
  const d = link.view ? { ...link.view.box, variable: link.view.variable } : info.default;
  Object.assign(state, { variable: d.variable, start: Date.parse(info.default.start + "T00:00:00Z"), days: info.default.days });
  if (link.view) Object.assign(state, { start: link.view.start, days: link.view.days });
  if (!isAnomaly()) Object.assign(state, { mode: "none", symmetric: false });
  if (link.mode) state.mode = link.mode;
  if (link.cmap) state.cmaps[state.variable] = link.cmap;
  if (link.range?.preset) state.preset = link.range.preset;
  if (link.range?.vmin != null) Object.assign(state, { preset: "custom", vmin: link.range.vmin, vmax: link.range.vmax });
  // one entry per variable: a twin with rivers and lakes, or with the L2's mean sea surface, is a choice in the
  // Display menu, not an entry
  const listed = Object.keys(info.variables).filter((k) => !(info.looks[k].twin && info.looks[k].water)
    && !(info.looks[k].mss && !info.looks[k].dtu) && !(info.looks[k].cal && !info.looks[k].calibrated));
  if (info.calibration) $("#cal-version").textContent = `experimental v${info.calibration.split(" ")[0]}`;
  $("#variable").innerHTML = listed.map((k) => `<option value="${k}">${info.variables[k]}</option>`).join("");
  showVariable();
  $("#link-button").onclick = copyLink;
  document.querySelectorAll(".max-days").forEach((e) => (e.textContent = info.max_days));
  $("#about-coverage").textContent = `${dayText(Date.parse(info.first))} to ${dayText(Date.parse(info.last))}, ` +
    `${info.passages.toLocaleString("en")} passages over ${info.passes} passes of the 21-day orbit`;
  $("#version").textContent = `${info.release} (code ${info.version})`;
  const at = new Date(info.commit.time + "Z");
  $("#product").textContent = `Sea surface height · ${info.product}` + (info.detail ? ` · ${info.detail.product} from zoom ${info.detail.zoom - 1}` : "") +
    ` · commit ${info.commit.id}, ${dayText(at)} ${at.toISOString().slice(11, 16)} UTC`;
  $("#product").title = `The store as of this commit (the latest when the server started): ${info.commit.message}`;
  const [url, brightness] = BASEMAPS["Bathymetry, gray"];
  // the projection remembered in this browser (globe by default), set before the first frame, and a zoom where
  // the globe fills the room between the panels (the flat world, at 0.7 less, looks as big)
  let saved = null;
  try { saved = localStorage.getItem("projection"); } catch (e) {}
  // on a small screen the panels cover the bottom and the top: the map centers in the room left between them
  const padding = narrow() ? { top: 56, bottom: $("#time").offsetHeight + $("#legend").offsetHeight + 24 } : { top: 0, bottom: 0 };
  const globe = link.map ? link.map.globe : saved !== "mercator", room = Math.min(innerWidth, innerHeight - padding.top - padding.bottom - (narrow() ? 20 : 160));
  const zoom = Math.min(2.3, Math.log2(room * 0.95 * Math.PI / 512)) - (globe ? 0 : 0.7);
  document.body.classList.toggle("globe", globe);
  // the opening region in view: the world, or a smaller default box (a product of small regions) filling the room
  const box = isGlobe(d) ? null : d, span = box && Math.max(((d.east - d.west) % 360 + 360) % 360, d.north - d.south);
  map = new maplibregl.Map({
    container: "map", center: link.map ? link.map.center : box ? [d.west + (((d.east - d.west) % 360 + 360) % 360) / 2, (d.south + d.north) / 2] : [20, 5],
    zoom: link.map ? link.map.zoom : box ? Math.log2(room * 360 / 512 / span) - 0.3 : zoom, minZoom: 0, maxZoom: 11, attributionControl: false,
    style: { version: 8, projection: { type: globe ? "globe" : "mercator" }, sources: { basemap: { type: "raster", tiles: [url], tileSize: 256, maxzoom: 9,
                                                attribution: `GEBCO, NOAA NCEI · SWOT: ${info.credit}` } },
             layers: [{ id: "basemap", type: "raster", source: "basemap",
                        paint: { "raster-brightness-max": brightness, "raster-fade-duration": 0 } }] },
  });
  map.setPadding({ ...padding, left: 0, right: 0 });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");  // in About on a small screen
  timeline = new Timeline($("#timeline"), Date.parse(info.first), Date.parse(info.last), setDates);
  timeline.setBand(state.start, state.days);
  setupDateButtons();
  overview.image.onload = drawOverview;
  overview.image.src = "/api/world.png";
  $("#overview").onclick = (e) => {  // go to the clicked place (approximately inverts the projection)
    const [X, Y] = info.world, x = (e.offsetX / 170 * 2 - 1) * X, y = (1 - e.offsetY / 83 * 2) * Y;
    const lat = Math.max(-80, Math.min(80, y / Y * 90)), lon = x / (X * Math.cos(lat * Math.PI / 180 * 0.8)) * 180;
    map.flyTo({ center: [Math.max(-180, Math.min(180, lon)), lat] });
  };
  map.once("style.load", () => {  // not "load": that waits for the basemap's tiles, which can take seconds
    setProjection(globe);
    $("#projection").onclick = () => setProjection(!document.body.classList.contains("globe"));
    map.addSource("box", { type: "geojson", data: { type: "Feature", geometry: { type: "Point", coordinates: [0, 0] } } });
    map.addLayer({ id: "box", type: "line", source: "box", paint: { "line-color": "#3b82f6", "line-width": 2 } });
    map.addSource("line", { type: "geojson", data: { type: "Feature", geometry: { type: "Point", coordinates: [0, 0] } } });
    map.addLayer({ id: "line", type: "line", source: "line", paint: { "line-color": "#f97316", "line-width": 2.5 } });
    map.addSource("probe", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "probe", type: "circle", source: "probe",
                   paint: { "circle-radius": 5, "circle-color": "#ffffff", "circle-stroke-color": "#f97316", "circle-stroke-width": 3 } });
    setupLine();
    setupDrawing();
    setupCoords();
    watchDetail();
    watchUnder();
    setupCmap();
    if (DEBUG) setupDebug();
    state.afterLoad = () => { track("open", { from_link: !!link.view }); if (link.point || link.line) restoreLink(link); };
    setBox({ west: d.west, east: d.east, south: d.south, north: d.north });
    drawOverview();
  });
  map.on("move", drawOverview);
  map.on("moveend", linkSoon);
  map.on("moveend", followArea);
  map.on("moveend", fitColors);
  map.on("zoomend", () => {  // into the 250 m (once per visit there)
    const fine = info.detail && map.getZoom() >= info.detail.zoom - 1 && info.looks[state.variable].fine;
    if (fine && !state.inFine) track("zoom_250m");
    state.inFine = fine;
  });
  addEventListener("pagehide", () => track("close"));
  map.on("click", (e) => {
    if (document.body.classList.contains("sheet")) document.body.classList.remove("sheet");  // a tap outside closes it
    else if (!state.drawing && !state.lining && !state.justDrew) openPoint(e.lngLat);
    state.justDrew = false;
  });
  setupMenus();
  $("#water").onchange = () => {  // the same variable with or without rivers and lakes (its twin)
    state.variable = info.looks[state.variable].twin;
    showVariable();
    loadData();
  };
  $("#cal").onchange = () => {  // the same variable calibrated or not
    state.variable = info.looks[state.variable].cal;
    showVariable();
    loadData();
  };
  $("#mss").onclick = (e) => {  // the same variable with the other mean sea surface
    const look = info.looks[state.variable];
    if (!e.target.dataset.value || (e.target.dataset.value === "dtu") === look.dtu) return;
    state.variable = look.mss;
    showVariable();
    loadData();
  };
  lucide.createIcons();
}
main().catch((e) => toast(e.message));
