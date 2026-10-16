// SWOT Sea Surface Height Explorer: the page. The server (server.py) reads the IceChunk store and draws tiles.
const $ = (s) => document.querySelector(s);
const DAY = 86400000;
const NCEI = "https://tiles.arcgis.com/tiles/C8EMgrsFcRFL6LrL/arcgis/rest/services/{}/MapServer/tile/{z}/{y}/{x}";
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{z}/{y}/{x}";
const BASEMAPS = {
  "Bathymetry, gray": [NCEI.replace("{}", "GEBCO_grayscale_basemap_NCEI"), 0.55],  // url, brightness (dark page)
  "Bathymetry, color": [NCEI.replace("{}", "GEBCO_basemap_NCEI"), 0.75],
  "Imagery": [ESRI.replace("{}", "World_Imagery"), 0.8],
};
const RDBU = ["#053061", "#2166ac", "#4393c3", "#92c5de", "#d1e5f0", "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b", "#67001f"];

const state = { mode: "box", preset: "2", symmetric: true, data: null, drawing: false };
let info, map, timeline;

// --- small helpers
async function api(path, params = {}) {
  const r = await fetch(path + (path.includes("?") ? "&" : "?") + new URLSearchParams(params));
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const wrap = (lon) => ((lon + 180) % 360 + 360) % 360 - 180;
const deg = (v, neg, pos, digits = 2) => `${+Math.abs(v).toFixed(digits)}°${v < 0 ? neg : v > 0 ? pos : ""}`;
const lonText = (lon, digits) => deg(wrap(lon), "W", "E", digits);
const latText = (lat, digits) => deg(lat, "S", "N", digits);
const dayText = (t) => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const isoDay = (t) => new Date(t).toISOString().slice(0, 10);
const timeText = (iso) => `${dayText(Date.parse(iso + "Z"))} ${iso.slice(11, 16)} UTC`;
const isGlobe = (b) => ((b.east - b.west) % 360 + 360) % 360 === 0 && b.south <= -78 && b.north >= 78;
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("#toast").classList.add("hidden"), 6000);
}

// --- loading data for the region and dates
async function loadData() {
  $("#busy").classList.remove("hidden");
  try {
    // every later request (tiles, values, download) names this view, so the server can read it again
    const view = new URLSearchParams({ ...state.box, start: isoDay(state.start), days: state.days, variable: state.variable });
    const d = await api("/api/load", view);
    state.data = d;
    state.view = view.toString();
    $("#stats").innerHTML = `<b>${d.passages}</b> passages · <b>${(d.pixels / 1e6).toFixed(1)} M</b> pixels · ` +
      (d.seconds ? `read from the cloud in <b>${d.seconds.toFixed(1)} s</b>` : "from the server's memory");
    $("#code").textContent = d.code;
    Prism.highlightElement($("#code"));
    if ($("#download")) $("#download").href = `/api/netcdf?${state.view}`;  // absent where SWOT_NETCDF=0
    setColors();
  } catch (e) {
    toast(e.message);
  } finally {
    $("#busy").classList.add("hidden");
  }
}

// --- colors: from percentiles of the loaded values, or typed
function setColors() {
  const q = state.data?.quantiles[state.mode] ?? [];
  if (state.preset !== "custom" && q.length) {
    const p = Number(state.preset) * 10;
    let [lo, hi] = [q[p], q[1000 - p]];
    if (state.symmetric) [lo, hi] = [-Math.max(-lo, hi), Math.max(-lo, hi)];
    [state.vmin, state.vmax] = [+lo.toFixed(3), +hi.toFixed(3)];
  }
  $("#colorbar").style.background = `linear-gradient(90deg, ${RDBU.join(", ")})`;
  $("#vmin").value = state.vmin;
  $("#vmax").value = state.vmax;
  $("#vmin-text").textContent = `${state.vmin} m`;
  $("#vmax-text").textContent = `${state.vmax} m`;
  const pct = (v) => { // percentile of a value, from the quantiles
    let i = q.findIndex((x) => x >= v);
    return i < 0 ? 100 : i / 10;
  };
  $("#percentiles").textContent = q.length ? `limits at percentiles ${pct(state.vmin)} and ${pct(state.vmax)}` : "";
  $("#legend-variable").textContent = info.variables[state.variable] + (state.mode === "none" ? "" :
    state.mode === "box" ? ", minus box mean" : ", minus passage means");
  document.querySelectorAll("#preset button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.preset));
  document.querySelectorAll("#mode button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.mode));
  $("#symmetric").checked = state.symmetric;
  if (!state.data) return;
  if (transect && transect.key !== `${state.view}|${state.mode}|${transect.step}`) fillTransect();
  const url = `/api/tile/{z}/{x}/{y}.png?${state.view}&v=${info.version}&mode=${state.mode}&vmin=${state.vmin}&vmax=${state.vmax}`;
  const source = map.getSource("swot");
  if (source) source.setTiles([url]);
  else {
    map.addSource("swot", { type: "raster", tiles: [url], tileSize: 256 });
    map.addLayer({ id: "swot", type: "raster", source: "swot", paint: { "raster-fade-duration": 0, "raster-resampling": "nearest" } }, "box");
  }
}

// --- the dates: from the timeline, or a click on a time series
function setDates(start, days) {
  Object.assign(state, { start, days });
  timeline.setBand(start, days);
  pin?.getElement().querySelector(".card").plot?.redraw();  // its shaded band
  loadData();
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
  state.drawing = !state.drawing;
  $("#draw").classList.toggle("active", state.drawing);
  map.getCanvas().style.cursor = state.drawing ? "crosshair" : "";
  state.drawing ? map.dragPan.disable() : map.dragPan.enable();
}

function setupDrawing() {
  let start = null;
  map.on("mousedown", (e) => { if (state.drawing) start = e.lngLat; });
  map.on("mousemove", (e) => {
    if (!start) return;
    const [w, e2, s, n] = [Math.min(start.lng, e.lngLat.lng), Math.max(start.lng, e.lngLat.lng),
                           Math.min(start.lat, e.lngLat.lat), Math.max(start.lat, e.lngLat.lat)];
    map.getSource("box").setData({ type: "Feature", geometry: { type: "LineString",
      coordinates: [[w, s], [e2, s], [e2, n], [w, n], [w, s]] } });
  });
  map.on("mouseup", (e) => {
    if (!start) return;
    const r = (v) => Math.round(v * 100) / 100;
    const box = { west: r(wrap(Math.min(start.lng, e.lngLat.lng))), east: r(wrap(Math.max(start.lng, e.lngLat.lng))),
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
    Object.assign(this, { canvas, onChange, ticks: { day: [], n: [], cycle: [] } });
    this.full = [Math.floor(first / DAY) * DAY, Math.ceil(last / DAY) * DAY + DAY];
    this.view = [...this.full];
    canvas.addEventListener("pointerdown", (e) => this.down(e));
    canvas.addEventListener("pointermove", (e) => this.move(e));
    canvas.addEventListener("pointerup", (e) => this.up(e));
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
    const d = new Date(this.view[0]);
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
    const n = Math.round((this.end - this.start) / DAY);
    $("#selection").textContent = n > 1 ? `${dayText(this.start)} – ${dayText(this.end - DAY)} · ${n} days` : `${dayText(this.start)} · 1 day`;
  }
  down(e) {
    const x = e.offsetX, [a, b] = [this.x(this.start), this.x(this.end)];
    this.drag = Math.abs(x - a) < 6 ? "start" : Math.abs(x - b) < 6 ? "end" : x > a && x < b ? "move" : "new";
    this.anchor = { x, start: this.start, end: this.end, day: this.day(x) };
    this.canvas.setPointerCapture(e.pointerId);
  }
  move(e) {
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
  up() {
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
    const t = this.t(e.offsetX), f = e.deltaY > 0 ? 1.25 : 0.8, [lo, hi] = this.full;
    let [a, b] = [t - (t - this.view[0]) * f, t + (this.view[1] - t) * f];
    if (b - a < 20 * DAY) return;
    [a, b] = b - a >= hi - lo ? [lo, hi] : a < lo ? [lo, lo + b - a] : b > hi ? [hi - (b - a), hi] : [a, b];
    this.view = [a, b];
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

// --- the time series at a clicked point: a card tied to the point by a line
let pin = null;
async function openPoint(lngLat) {  // a small card: where, and the value there; the time series on request
  pin?.remove();
  const p = map.project(lngLat), size = map.getContainer().getBoundingClientRect();
  const right = p.x < size.width * 0.55, below = p.y < size.height * 0.45;
  const el = document.createElement("div");
  el.className = "pin";
  el.innerHTML = `<svg><line x1="0" y1="0"/></svg>
    <div class="dot"></div>
    <div class="card compact">
      <header><div><h3>${latText(lngLat.lat)} ${lonText(lngLat.lng)}</h3>
        <p class="hint"><span class="spinner"></span></p></div>
        <div class="tools"><select class="hidden" title="Mean of the pixels within this distance">
          ${[2, 5, 10, 20].map((r) => `<option value="${r}" ${r === 5 ? "selected" : ""}>${r} km</option>`).join("")}</select>
          <button class="ghost close" title="Close"><i data-lucide="x"></i></button></div></header>
      <button class="expand" title="Mean around this point, every passage of the mission"><i data-lucide="chart-line"></i>Time series</button>
      <div class="chart"></div></div>`;
  const card = el.querySelector(".card"), radius = card.querySelector("select");
  for (const ev of ["mousedown", "mouseup", "click", "dblclick", "wheel", "pointerdown"]) card.addEventListener(ev, (e) => e.stopPropagation());
  card.querySelector(".close").onclick = () => { pin.remove(); pin = null; };
  radius.onchange = () => fillSeries(card, lngLat, Number(radius.value));
  // the line goes from the point to the card's nearest corner; the card can be moved (by its header) and resized
  const line = el.querySelector("line");
  const link = () => {
    const [l, t, w, h] = [card.offsetLeft, card.offsetTop, card.offsetWidth, card.offsetHeight];
    line.setAttribute("x2", Math.min(Math.max(0, l), l + w));
    line.setAttribute("y2", Math.min(Math.max(0, t), t + h));
  };
  const place = (w, h, first = false) => {  // on the side with room; when it grows, the corner near the point stays
    const x = first ? (right ? 40 : -40) : card.offsetLeft + (right ? 0 : card.offsetWidth);
    const y = first ? (below ? 40 : -40) : card.offsetTop + (below ? 0 : card.offsetHeight);
    card.style.left = `${right ? x : x - w}px`;
    card.style.top = `${below ? y : y - h}px`;
  };
  dragByHeader(card, link);
  card.querySelector(".expand").onclick = () => {
    const [w, h] = [440, 250];
    place(w, h);
    card.classList.remove("compact");
    Object.assign(card.style, { width: `${w}px`, height: `${h}px` });
    card.querySelector("h3").textContent += ` · ${info.variables[state.variable]}`;
    radius.classList.remove("hidden");
    fillSeries(card, lngLat, 5);
  };
  new ResizeObserver(() => {
    link();
    card.plot?.setSize(chartSize(card));
  }).observe(card);
  pin = new maplibregl.Marker({ element: el, anchor: "top-left" }).setLngLat(lngLat).addTo(map);
  lucide.createIcons();
  place(card.offsetWidth, card.offsetHeight, true);
  link();
  const v = state.data ? await api(`/api/value?${state.view}`, { lon: wrap(lngLat.lng), lat: lngLat.lat, mode: state.mode })
    .catch(() => ({})) : {};
  const bottom = card.offsetTop + card.offsetHeight;  // the text may take more lines: keep the edge near the point
  const mean = v.passages?.length > 1 ? ` (mean of ${v.passages.length} passages)` : "";
  card.querySelector(".hint").textContent = v.value == null ? "No data here on the selected dates"
    : [`${v.value.toFixed(3)} m${mean} · ${$("#legend-variable").textContent}`,
       ...v.passages.map((q) => `Pass ${q.pass_id} · cycle ${q.cycle} · ${timeText(q.time)}`)].join("\n");
  if (!below) card.style.top = `${bottom - card.offsetHeight}px`;
  link();
}

function chartSize(card) {  // the chart fills the card below its header
  return { width: card.clientWidth - 24, height: card.clientHeight - card.querySelector("header").offsetHeight - 26 };
}

async function fillSeries(card, lngLat, radius) {
  const chart = card.querySelector(".chart"), hint = card.querySelector(".hint");
  card.plot?.destroy();
  card.plot = null;
  chart.innerHTML = `<div class="loading"><span class="spinner"></span>Reading the whole mission from the cloud…</div>`;
  hint.textContent = `mean within ${radius} km`;
  try {
    const s = await api("/api/series", { lon: wrap(lngLat.lng), lat: lngLat.lat, variable: state.variable, radius });
    hint.textContent = `mean within ${radius} km · ${s.time.length} passages · click one to see that date on the map`;
    chart.innerHTML = "";
    if (!s.time.length) { chart.innerHTML = `<div class="loading">No passage here (outside the swaths, or in the nadir gap).</div>`; return; }
    const band = { hooks: { drawClear: [(u) => {  // the selected dates
      const [a, b] = [u.valToPos(state.start / 1000, "x", true), u.valToPos((state.start + state.days * DAY) / 1000, "x", true)];
      u.ctx.fillStyle = "rgba(249,115,22,0.25)";
      u.ctx.fillRect(a, u.bbox.top, Math.max(b - a, 2), u.bbox.height);
    }] } };
    const axis = { stroke: "#94a3b8", grid: { stroke: "rgba(148,163,184,0.12)" }, ticks: { stroke: "rgba(148,163,184,0.25)" } };
    const tip = document.createElement("div");  // date and value of the point under the mouse
    tip.className = "tip";
    const hover = { hooks: { init: [(u) => {
      u.over.appendChild(tip);
      u.over.addEventListener("click", () => {  // that passage's date on the map, keeping the number of days
        if (u.cursor.idx == null) return;
        const day = Math.floor(u.data[0][u.cursor.idx] * 1000 / DAY) * DAY;
        const start = Math.min(Math.max(day - Math.floor((state.days - 1) / 2) * DAY, timeline.full[0]),
                               timeline.full[1] - state.days * DAY);
        setDates(start, state.days);
      });
    }], setCursor: [(u) => {
      const i = u.cursor.idx;
      tip.style.display = i == null ? "none" : "block";
      if (i == null) return;
      const x = u.valToPos(u.data[0][i], "x"), y = u.valToPos(u.data[1][i], "y");
      Object.assign(tip.style, { left: `${x}px`, top: `${y}px`,
                                 transform: `translate(${x > u.over.clientWidth * 0.75 ? "-100%" : "-50%"}, -140%)` });
      tip.textContent = `${dayText(u.data[0][i] * 1000)} · ${u.data[1][i].toFixed(3)} m`;
    }] } };
    card.plot = new uPlot({
      ...chartSize(card), plugins: [band, hover], legend: { show: false }, cursor: { points: { size: 7 } },
      scales: { x: { time: true } },
      axes: [axis, { ...axis, label: "m", size: 45 }],
      series: [{}, { stroke: "#3b82f6", width: 1, points: { show: true, size: 4, fill: "#3b82f6" } }],
    }, [s.time.map((t) => t / 1000), s.value], chart);
  } catch (e) {
    chart.innerHTML = `<div class="loading">${e.message}</div>`;
  }
}

// --- mouse position (and value) in the lower right corner
function setupCoords() {
  let pending = null;
  map.on("mousemove", (e) => {
    const where = `${latText(e.lngLat.lat, 3)}  ${lonText(e.lngLat.lng, 3)}`;
    $("#coords").textContent = where;
    if (!state.data || pending) return;
    pending = setTimeout(async () => {  // at most a few value requests per second
      pending = null;
      const v = await api(`/api/value?${state.view}`, { lon: wrap(e.lngLat.lng), lat: e.lngLat.lat, mode: state.mode }).catch(() => ({}));
      if (v.value != null && $("#coords").textContent.startsWith(where)) $("#coords").textContent = `${where} · ${v.value.toFixed(3)} m`;
    }, 150);
  });
  map.getCanvas().addEventListener("mouseleave", () => { $("#coords").textContent = ""; });
}

// --- a line drawn on the map: the values along it, one curve per passage of the selected dates
const PALETTE = ["#3b82f6", "#f97316", "#22c55e", "#e879f9", "#facc15", "#14b8a6", "#f43f5e", "#a78bfa"];
let transect = null;  // { points, step, card, key }

function drawLine() {  // the next clicks on the map are its vertices; a double click ends it
  if (state.drawing) drawBox();
  state.lining = !state.lining;
  state.vertices = [];
  $("#line").classList.toggle("active", state.lining);
  map.getCanvas().style.cursor = state.lining ? "crosshair" : "";
  state.lining ? map.doubleClickZoom.disable() : map.doubleClickZoom.enable();
  if (!state.lining) showLine(transect?.points ?? []);
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
    state.vertices.push([e.lngLat.lng, e.lngLat.lat]);
    showLine(state.vertices);
  });
  map.on("mousemove", (e) => { if (state.lining && state.vertices.length) showLine(state.vertices, [e.lngLat.lng, e.lngLat.lat]); });
  map.on("dblclick", (e) => {
    if (!state.lining) return;
    e.preventDefault();
    const points = state.vertices;
    drawLine();
    if (points.length > 1) openTransect(points);
  });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && state.lining) drawLine(); });
}

function openTransect(points) {
  transect?.card.remove();
  const card = document.createElement("section");
  card.className = "card transect";
  card.innerHTML = `<header><div><h3>Along the line</h3><p class="hint"><span class="spinner"></span></p></div>
      <div class="tools"><select title="Distance between the points of the line">
        ${[1, 2, 5, 10].map((k) => `<option value="${k}" ${k === 2 ? "selected" : ""}>every ${k} km</option>`).join("")}</select>
        <button class="ghost close" title="Close"><i data-lucide="x"></i></button></div></header>
    <div class="chart"></div><div class="passages"></div>`;
  document.body.appendChild(card);
  lucide.createIcons();
  transect = { points, step: 2, card };
  card.querySelector(".close").onclick = () => { card.remove(); transect = null; showLine([]); showProbe(null); };
  card.querySelector("select").onchange = (e) => { transect.step = Number(e.target.value); fillTransect(); };
  dragByHeader(card);
  new ResizeObserver(() => card.plot?.setSize(transectSize(card))).observe(card);
  showLine(points);
  fillTransect();
}

function transectSize(card) {
  return { width: card.clientWidth - 24,
           height: card.clientHeight - card.querySelector("header").offsetHeight - card.querySelector(".passages").offsetHeight - 30 };
}

async function fillTransect() {
  const { card, points, step } = transect, chart = card.querySelector(".chart"), hint = card.querySelector(".hint");
  transect.key = `${state.view}|${state.mode}|${step}`;
  card.plot?.destroy();
  card.plot = null;
  chart.innerHTML = `<div class="loading"><span class="spinner"></span>Reading the values along the line…</div>`;
  card.querySelector(".passages").innerHTML = "";
  try {
    const t = await api(`/api/transect?${state.view}`, { mode: state.mode, step,
      points: points.map(([x, y]) => `${wrap(x).toFixed(4)},${y.toFixed(4)}`).join(";") });
    card.querySelector("h3").textContent = `Along the line · ${t.distance.at(-1).toFixed(0)} km · ${$("#legend-variable").textContent}`;
    hint.textContent = `nearest pixel of each passage, every ${t.step} km · hover to see where`;
    chart.innerHTML = "";
    if (!t.passages.length) {
      chart.innerHTML = `<div class="loading">No passage crosses the line on the selected dates.</div>`;
      return;
    }
    const color = (k) => PALETTE[k % PALETTE.length];
    card.querySelector(".passages").innerHTML = t.passages.map((q, k) =>
      `<span><i style="background:${color(k)}"></i>Pass ${q.pass_id} · cycle ${q.cycle} · ${timeText(q.time)}</span>`).join("");
    const axis = { stroke: "#94a3b8", grid: { stroke: "rgba(148,163,184,0.12)" }, ticks: { stroke: "rgba(148,163,184,0.25)" } };
    const tip = document.createElement("div");
    tip.className = "tip";
    const hover = { hooks: { init: [(u) => u.over.appendChild(tip)], setCursor: [(u) => {
      const i = u.cursor.idx;
      tip.style.display = i == null ? "none" : "block";
      showProbe(i == null ? null : [t.lon[i], t.lat[i]]);
      if (i == null) return;
      const x = u.valToPos(t.distance[i], "x");
      Object.assign(tip.style, { left: `${x}px`, top: "4px", transform: `translateX(${x > u.over.clientWidth * 0.6 ? "-105%" : "5%"})` });
      const here = t.passages.map((q, k) => q.value[i] == null ? "" : `<div><i style="background:${color(k)}"></i>${q.value[i].toFixed(3)} m</div>`);
      tip.innerHTML = `<b>${t.distance[i].toFixed(0)} km</b>${here.join("") || "<div>no data</div>"}`;
    }] } };
    card.plot = new uPlot({
      ...transectSize(card), plugins: [hover], legend: { show: false }, cursor: { points: { size: 6 } },
      scales: { x: { time: false } },
      axes: [{ ...axis, values: (u, ticks) => ticks.map((v) => `${v} km`) }, { ...axis, label: "m", size: 45 }],
      series: [{}, ...t.passages.map((q, k) => ({ stroke: color(k), width: 1.5 }))],
    }, [t.distance, ...t.passages.map((q) => q.value)], chart);
  } catch (e) {
    chart.innerHTML = `<div class="loading">${e.message}</div>`;
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
  $("#draw").onclick = () => { if (state.lining) drawLine(); drawBox(); };
  $("#line").onclick = drawLine;
  $("#variable").onchange = () => { state.variable = $("#variable").value; state.symmetric = state.variable !== "adt" || state.mode !== "none"; loadData(); };
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
  $("#code-button").onclick = () => $("#code-dialog").showModal();
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
  lucide.createIcons();
  try { localStorage.setItem("projection", globe ? "globe" : "mercator"); } catch (e) {}
  drawOverview();
}

// --- start
async function main() {
  info = await api("/api/info");
  const d = info.default;
  Object.assign(state, { variable: d.variable, start: Date.parse(d.start + "T00:00:00Z"), days: d.days });
  $("#variable").innerHTML = Object.entries(info.variables).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $("#variable").value = state.variable;
  document.querySelectorAll(".max-days").forEach((e) => (e.textContent = info.max_days));
  $("#about-coverage").textContent = `${dayText(Date.parse(info.first))} to ${dayText(Date.parse(info.last))}, ` +
    `${info.passages.toLocaleString("en")} passages over ${info.passes} passes of the 21-day orbit`;
  const [url, brightness] = BASEMAPS["Bathymetry, gray"];
  map = new maplibregl.Map({
    container: "map", center: [20, 5], zoom: 1.6, minZoom: 0.5, maxZoom: 11, attributionControl: false,
    style: { version: 8, sources: { basemap: { type: "raster", tiles: [url], tileSize: 256, maxzoom: 9,
                                                attribution: "GEBCO, NOAA NCEI · SWOT: AVISO/DUACS, CNES" } },
             layers: [{ id: "basemap", type: "raster", source: "basemap",
                        paint: { "raster-brightness-max": brightness, "raster-fade-duration": 0 } }] },
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  timeline = new Timeline($("#timeline"), Date.parse(info.first), Date.parse(info.last), setDates);
  timeline.setBand(state.start, state.days);
  overview.image.onload = drawOverview;
  overview.image.src = "/api/world.png";
  $("#overview").onclick = (e) => {  // go to the clicked place (approximately inverts the projection)
    const [X, Y] = info.world, x = (e.offsetX / 170 * 2 - 1) * X, y = (1 - e.offsetY / 83 * 2) * Y;
    const lat = Math.max(-80, Math.min(80, y / Y * 90)), lon = x / (X * Math.cos(lat * Math.PI / 180 * 0.8)) * 180;
    map.flyTo({ center: [Math.max(-180, Math.min(180, lon)), lat] });
  };
  map.on("load", () => {
    let saved = null;
    try { saved = localStorage.getItem("projection"); } catch (e) {}
    setProjection(saved !== "mercator");
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
    setBox({ west: d.west, east: d.east, south: d.south, north: d.north });
    drawOverview();
  });
  map.on("move", drawOverview);
  map.on("click", (e) => {
    if (!state.drawing && !state.lining && !state.justDrew) openPoint(e.lngLat);
    state.justDrew = false;
  });
  setupMenus();
  lucide.createIcons();
}
main().catch((e) => toast(e.message));
