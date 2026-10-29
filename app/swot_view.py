"""Data side of the viewer: the plain xarray recipes from the store docstring, nothing else.
It needs only icechunk + xarray (and arraylake for a store on Earthmover), like any reader, so `snippet`
can show the same code to users."""
import os

import icechunk
import numpy as np
import xarray as xr

STORE = os.environ.get("SWOT_STORE", "s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2")
EARTH_RADIUS = 6371.0  # km


def open_repo(target=STORE):
    """arraylake:ORG/REPO (Earthmover; the client gets the storage credentials from an Arraylake token),
    s3://bucket/prefix (credentials and region from AWS_* in the environment) or a local path."""
    if target.startswith("arraylake:"):
        import arraylake
        import numcodecs.zarr3  # noqa: F401 (registers the codecs of the L2 stores, e.g. pcodec)

        return arraylake.Client().get_repo(target.removeprefix("arraylake:"))
    if target.startswith("s3://"):
        bucket, prefix = target.removeprefix("s3://").split("/", 1)
        return icechunk.Repository.open(icechunk.s3_storage(bucket=bucket, prefix=prefix, from_env=True))
    return icechunk.Repository.open(icechunk.local_filesystem_storage(target))


def dataset(store):
    return xr.open_zarr(store, consolidated=False, chunks=None).set_xindex("lat")


def open_store(target=STORE, branch="main"):
    """The dataset of a branch's latest commit."""
    return dataset(open_repo(target).readonly_session(branch).store)


def written(ds, store):
    """The passages (time indices) of a store being filled that have data: flagged 1 in its `written` (0: not yet,
    2: skipped, no position), or in stores without it, those in a chunk of `line_time` that exists. Listing the chunks
    reads no data (the store is created for every passage; an unwritten chunk is absent). With chunks of several
    passages along time (not in the stores being filled), all of a written chunk count."""
    if "written" in ds:
        return np.flatnonzero(ds.written.values == 1)
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def listing():
        return [int(k) async for k in store.list_dir("line_time/c")]

    with ThreadPoolExecutor(1) as pool:  # its own event loop, also when called from one (a server starting)
        chunks = pool.submit(asyncio.run, listing()).result()
    per = ds.line_time.encoding["chunks"][0]
    return np.array(sorted(i for k in chunks for i in range(k * per, min((k + 1) * per, ds.sizes["time"]))), int)


def nadir_envelope(ds, half_width=70.0, passes=None):
    """Each pass's west/east ends of its lines from its first passage's nadir and the swath's half width (km), for
    stores whose envelopes (lon_min/lon_max) are not written yet: (pass_number, along) arrays like those, NaN for
    the passes not asked (`passes`: all by default). The nadirs are read in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    lon_min = np.full(ds.lon_min.shape, np.nan, "float32")
    lon_max = np.full(ds.lon_max.shape, np.nan, "float32")
    first = {}
    for i, p in enumerate(ds.pass_id.values):
        first.setdefault(int(p), i)
    if passes is not None:
        first = {p: i for p, i in first.items() if p in set(int(q) for q in passes)}
    numbers = list(ds.pass_number.values)

    def one(item):
        p, i = item
        nadir = ds[["latitude_nadir", "longitude_nadir"]].isel(time=i).load()
        dlon = half_width / (EARTH_RADIUS * np.pi / 180 * np.cos(np.deg2rad(nadir.latitude_nadir.values)))
        k = numbers.index(p)
        lon_min[k] = nadir.longitude_nadir.values - dlon
        lon_max[k] = nadir.longitude_nadir.values + dlon

    with ThreadPoolExecutor(32) as pool:
        list(pool.map(one, first.items()))
    return lon_min, lon_max


def passes_over(ds, west, east, south, north):
    """Numbers of the passes whose swath crosses the box; reads only lon_min/lon_max."""
    geo = ds[["lon_min", "lon_max"]].sel(lat=slice(south, north))
    first = (geo.lon_min - west) % 360  # how far east of `west` each line starts
    width = (east - west) % 360 or 360  # west == east (e.g. -180 to 180) is the whole globe
    hit = ((first <= width) | (first + geo.lon_max - geo.lon_min >= 360)).any("along")
    return geo.pass_number[hit].values


def subset(ds, west, east, south, north, start=None, end=None):
    """Passages crossing the box in [start, end], cut to its latitudes (not loaded)."""
    sub = ds.sel(time=slice(start, end), lat=slice(south, north))
    return sub.isel(time=sub.pass_id.isin(passes_over(ds, west, east, south, north)).values)


def distance(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    lat1, lon1, lat2, lon2 = map(np.deg2rad, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS * np.arcsin(np.sqrt(a))


CAL = ("cal_bias", "cal_slope")


def per_passage(sub, names):
    """A subset's arrays `names`, those kept once per pass (pass_number, e.g. Expert's mss_cor_dtu25) taken at each
    passage's pass: in memory."""
    passes = np.unique(sub.pass_id.values)
    return {n: sub[n].sel(pass_number=passes).load().sel(pass_number=sub.pass_id) if "pass_number" in sub[n].dims
            else sub[n].load() for n in names}


def calibrated(sub, value):
    """`value` minus the experimental calibration (swot_cloud v0.1: cal_bias and cal_slope, each line's per side),
    bias + slope x cross / 60 on each pixel's side of nadir."""
    east = sub.cal_bias.isel(side=1) + sub.cal_slope.isel(side=1) * sub.cross / 60
    west = sub.cal_bias.isel(side=0) + sub.cal_slope.isel(side=0) * sub.cross / 60
    return value - xr.where(sub.cross > 0, east, west)


def point_series(ds, lon, lat, var="ssha_filtered", radius=5.0, min_pixels=None, mask=None, db=False, surface=None,
                 calibrate=False):
    """Mean of `var` (or of the sum of a list of arrays: "value"; `calibrate`: minus the calibration) over the pixels
    within `radius` km of (lon, lat), one value per passage (with `mask`, a quality flag: only where it is 0; `db`: of its values in dB, as sigma0 is shown; `surface`: only on these classes
    of ancillary_surface_classification_flag, e.g. (0,) the open ocean). Passages with fewer than `min_pixels` valid pixels there (nadir gap, swath
    edge) are dropped: by default a quarter of what the circle holds on the 2 km grid, at least 1 (5 km: 5)."""
    if min_pixels is None:
        min_pixels = max(1, round(np.pi * radius**2 / 4 / 4))
    km_per_deg = EARTH_RADIUS * np.pi / 180
    dlon = radius / (km_per_deg * np.cos(np.deg2rad(lat)))
    # lat(along) is the nadir latitude: swath edges lean up to ~0.3 deg away from it
    dlat = radius / km_per_deg + 0.5
    surf = "ancillary_surface_classification_flag"
    names, name = ([var], var) if isinstance(var, str) else (list(var), "value")
    sub = subset(ds, lon - dlon, lon + dlon, lat - dlat, lat + dlat)
    arrays = per_passage(sub, names + ([surf] if surface else []))  # surface classes: one per pass in Expert
    classes = arrays.pop(surf, None)
    sub = sub[["latitude", "longitude"] + ([mask] if mask else []) + (list(CAL) if calibrate else [])].load()
    value = sum(arrays.values())
    if calibrate:
        value = calibrated(sub, value)
    near = value.where(distance(sub.latitude, sub.longitude, lat, lon) <= radius)
    if mask:
        near = near.where(sub[mask] == 0)
    if surface:
        near = near.where(classes.isin(list(surface)))
    if db:
        near = 10 * np.log10(near.where(near > 0))
    count = near.count(("along", "cross"))
    out = xr.Dataset({name: near.mean(("along", "cross")), "pixels": count})
    return out.isel(time=(count >= min_pixels).values)


def repository(target):
    """The library, how to get access, and the code that opens a store's repository."""
    if target.startswith("arraylake:"):
        return ("arraylake", "an Arraylake token: run `arraylake auth login` once",
                f'arraylake.Client().get_repo("{target.removeprefix("arraylake:")}")')
    bucket, prefix = target.removeprefix("s3://").split("/", 1)
    return ("icechunk", "AWS credentials from the environment (AWS_PROFILE or AWS_ACCESS_KEY_ID...), region us-east-1",
            f'icechunk.Repository.open(icechunk.s3_storage(bucket="{bucket}", prefix="{prefix}", from_env=True))')


def opening(target=STORE, imports=""):
    """The first lines of every code snippet: the imports, then open the repository and the dataset."""
    library, how, repo = repository(target)
    return f'''import {library}
import xarray as xr
{imports}
# {how}
repo = {repo}
ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None).set_xindex("lat")
'''


def snippet(west, east, south, north, start, end, var="ssha_filtered", target=STORE, dtu=False, cal=False):
    """The code a user runs to get the same subset, with icechunk + xarray only. The longitude filter and
    the latitude cut are left out when they keep everything (west == east, e.g. -180 to 180, is all longitudes).
    `dtu`: plus the store's correction to the DTU25 mean sea surface, each passage's pass's; `cal`: minus its
    calibration."""
    # the generated code is text at column 0
    lats = "" if south <= -90 and north >= 90 else f", lat=slice({south:g}, {north:g})"
    lons = "" if (east - west) % 360 == 0 else f'''west, east = {west:g}, {east:g}
first = (sub.lon_min - west) % 360  # how far east of `west` each line starts
hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)
'''
    corrected = (f"ssha = sub.{var}\n" if dtu or cal else "") + ("""# the DTU25 mean sea surface instead of the L2's (CNES_CLS22): the correction, kept once per pass
ssha = ssha + ds.mss_cor_dtu25.sel(pass_number=np.unique(sub.pass_id), along=sub.along.values).load().sel(pass_number=sub.pass_id)
""" if dtu else "") + ("""# the experimental calibration (swot_cloud v0.1, may change): each line's bias and cross-track slope, on each
# pixel's side of nadir
east = sub.cal_bias.isel(side=1) + sub.cal_slope.isel(side=1) * sub.cross / 60
west = sub.cal_bias.isel(side=0) + sub.cal_slope.isel(side=0) * sub.cross / 60
ssha = ssha - xr.where(sub.cross > 0, east, west)
""" if cal else "")
    arrays = ", ".join(f'"{n}"' for n in [var] + (["cal_bias", "cal_slope"] if cal else []))
    return f'''{(DTU25_CREDIT if dtu else "") + opening(target, "import numpy as np\n" if dtu else "")}
sub = ds.sel(time=slice("{start}", "{end}"){lats})
{lons}sub = sub[[{arrays}]].load()  # dims (time, along, cross); latitude/longitude come along
{corrected}'''


DTU25_HERE = '''

def dtu25(sub):
    """The DTU25 mean sea surface instead of the L2's (CNES_CLS22): the correction, kept once per pass."""
    return sub.mss_cor_dtu25.sel(pass_number=np.unique(sub.pass_id)).load().sel(pass_number=sub.pass_id)
'''
DTU25_FROM_EXPERT = '''

def dtu25_250m(sub):
    """The Unsmoothed store has no MSS: the Expert's correction (ds) to the DTU25 mean sea surface, interpolated at the
    pixels' along and cross (both stores count them in km from the same reference track), past its edges too."""
    near = ds.mss_cor_dtu25.sel(along=slice(float(sub.along[0]) - 4, float(sub.along[-1]) + 4)).drop_vars("lat")
    return xr.concat([near.sel(pass_number=p).interp(along=sub.along.values, cross=sub.cross.values,
                                                     kwargs={"fill_value": None}).reset_coords(drop=True)
                      for p in sub.pass_id.values], "time")
'''
CAL_HERE = '''

def calibration(sub):
    """The experimental calibration (swot_cloud v0.1, may change), subtracted: each line's bias and cross-track slope,
    on each pixel's side of nadir."""
    cal = sub[["cal_bias", "cal_slope"]].load()
    east = cal.cal_bias.isel(side=1) + cal.cal_slope.isel(side=1) * sub.cross / 60
    west = cal.cal_bias.isel(side=0) + cal.cal_slope.isel(side=0) * sub.cross / 60
    return -xr.where(sub.cross > 0, east, west)
'''
CAL_FROM_EXPERT = '''

def calibration_250m(sub):
    """The Unsmoothed store has no calibration yet: the Expert's (ds, experimental v0.1) of the passage with the same
    cycle and pass, interpolated along track, at each pixel's distance to nadir (its line was moved by whole 250 m
    columns: cross_shift), subtracted."""
    row = {(c, p): i for i, (c, p) in enumerate(zip(ds.cycle.values, ds.pass_id.values))}
    near = ds[["cal_bias", "cal_slope"]].sel(along=slice(float(sub.along[0]) - 4, float(sub.along[-1]) + 4)).drop_vars("lat")
    def one(c, p):  # NaN if the Expert has no such passage
        i = row.get((c, p))
        e = near.isel(time=i or 0).interp(along=sub.along.values, kwargs={"fill_value": None}).reset_coords(drop=True)
        return e if i is not None else e * np.nan

    cal = xr.concat([one(c, p) for c, p in zip(sub.cycle.values, sub.pass_id.values)], "time")
    b = sub.cross - 0.25 * sub.cross_shift.fillna(0)
    east = cal.cal_bias.isel(side=1) + cal.cal_slope.isel(side=1) * b / 60
    west = cal.cal_bias.isel(side=0) + cal.cal_slope.isel(side=0) * b / 60
    return -xr.where(b > 0, east, west)
'''
DTU25_CREDIT = "# DTU25MSS: DTU Space (Nilsson, Andersen et al., ESSD 18, 5027, 2026), doi:10.11583/DTU.29412275, CC BY 4.0\n"


def series_snippet(lon, lat, names, radius=5.0, label="", target=STORE, mask=None, pixel=2.0, fine=None, db=False,
                   units="m", dtu=False, cal=False):
    """The code for the time series at a point (as `point_series`), with a figure: the mean within `radius` km of
    the product's `pixel` km pixels (with `mask`, a quality flag: only where it is 0; `dtu`: plus the store's
    correction to the DTU25 mean sea surface; `cal`: minus its calibration). `fine`, a dict of the same for a finer
    product (target, names, radius, mask, pixel, label, dtu and cal: the first store's, interpolated), adds its
    series to the figure."""
    fine_dtu, fine_cal = bool(fine and fine.get("dtu")), bool(fine and fine.get("cal"))
    curves = [(target, names, radius, mask, pixel, "", db, ["dtu25"] * dtu + ["calibration"] * cal)] + (
        [(fine["target"], fine["names"], fine["radius"], fine.get("mask"), fine["pixel"], fine["label"],
          fine.get("db", False), ["dtu25_250m"] * fine_dtu + ["calibration_250m"] * fine_cal)] if fine else [])
    calls = []
    for k, (where, arrays, r, flag, size, name, decibels, correction) in enumerate(curves):
        more = f", corrections=[{', '.join(correction)}]" if correction else ""
        if k == 0:
            calls.append(f"series = point_series(ds, {list(arrays)}, {r:g}, {size:g}, {flag!r}, {decibels!r}{more})")
        else:
            library, _, repo = repository(where)
            calls.append(f"""
# {name}: the same, within {r:g} km
{"import " + library + chr(10) if library not in opening(target) else ""}repo = {repo}
fine = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None).set_xindex("lat")
fine = point_series(fine, {list(arrays)}, {r:g}, {size:g}, {flag!r}, {decibels!r}{more})""")
    plots = (f"""series.plot(marker=".", label="{pixel:g} km pixels, within {radius:g} km")
fine.plot(marker=".", label="{fine['label']}, within {fine['radius']:g} km")
plt.legend()""" if fine else 'series.plot(marker=".")')
    corrections = (DTU25_HERE * dtu + CAL_HERE * cal + DTU25_FROM_EXPERT * fine_dtu + CAL_FROM_EXPERT * fine_cal)
    return f'''{(DTU25_CREDIT if dtu else "") + opening(target, "import numpy as np\nimport matplotlib.pyplot as plt\n")}
lon, lat = {lon:.4f}, {lat:.4f}
km = 6371.0 * np.pi / 180
{corrections}

def point_series(ds, names, radius, pixel, flag=None, db=False, corrections=()):
    """The mean of the sum of `names` (plus each of `corrections(sub)`) over the pixels within `radius` km of the
    point, one value per passage (`db`: of the values in dB)."""
    # every passage over the point (the lines near it)
    west, east = lon - radius / (km * np.cos(np.deg2rad(lat))), lon + radius / (km * np.cos(np.deg2rad(lat)))
    sub = ds.sel(lat=slice(lat - radius / km - 0.5, lat + radius / km + 0.5))  # swath edges lean ~0.3 deg off nadir
    first = (sub.lon_min - west) % 360
    hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
    sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)
    value = sum(sub[n].load() for n in names) + sum(c(sub) for c in corrections)
    sub = sub[["latitude", "longitude"] + ([flag] if flag else [])].load()
    la, lo = np.deg2rad(sub.latitude), np.deg2rad(sub.longitude - lon)
    d = 2 * 6371.0 * np.arcsin(np.sqrt(np.sin((la - np.deg2rad(lat)) / 2) ** 2
                                       + np.cos(la) * np.cos(np.deg2rad(lat)) * np.sin(lo / 2) ** 2))
    near = value.where(d <= radius)
    if flag:  # only the pixels of good quality
        near = near.where(sub[flag] == 0)
    if db:
        near = 10 * np.log10(near.where(near > 0))
    count = near.count(("along", "cross"))
    # passages with too few pixels there (swath edge, nadir gap) are left out: a quarter of what the circle holds
    return near.mean(("along", "cross")).where(count >= max(1, round(np.pi * radius**2 / pixel**2 / 4))).dropna("time")


{chr(10).join(calls)}

plt.figure(figsize=(9, 4))
{plots}
plt.title("{label}{"" if fine else f"mean within {radius:g} km of "}{lat:.2f}, {lon:.2f}")
plt.ylabel("{units}")
plt.show()
'''


LINE_EXPERT = '''
expert = {repo}
expert = xr.open_zarr(expert.readonly_session("main").store, consolidated=False, chunks=None)
'''
LINE_DTU25_HERE = '''

def dtu25(times, row, col):
    """The DTU25 mean sea surface instead of the L2's (CNES_CLS22): the pass's correction at its pixels (row, col),
    the same every cycle."""
    p = int(sub.pass_id.values[times[0]])
    return np.tile(sub.mss_cor_dtu25.sel(pass_number=p).values[row, col], (len(times), 1))
'''
LINE_DTU25_FROM_EXPERT = '''

def dtu25(times, row, col):
    """The Unsmoothed store has no MSS: the Expert's correction to the DTU25 mean sea surface, interpolated at the
    pixels' along and cross (both stores count them in km from the same reference track), past its edges too."""
    p = int(sub.pass_id.values[times[0]])
    at = dict(along=xr.DataArray(sub.along.values[row], dims="pixel"), cross=xr.DataArray(sub.cross.values[col], dims="pixel"))
    near = expert.mss_cor_dtu25.sel(pass_number=p, along=slice(float(at["along"].min()) - 4, float(at["along"].max()) + 4))
    return np.tile(near.interp(at, kwargs={"fill_value": None}).values, (len(times), 1))
'''
LINE_CAL_HERE = '''

def calibration(times, row, col):
    """The experimental calibration (swot_cloud v0.1, may change), subtracted: each passage's bias and cross-track
    slope of the lines `row`, on each pixel's side of nadir."""
    at = dict(time=xr.DataArray(times, dims="passage"), along=xr.DataArray(row, dims="pixel"))
    bias, slope = (sub[n].isel(at).values for n in ("cal_bias", "cal_slope"))  # (passages, pixels, side)
    cross = sub.cross.values[col]
    side, k = (cross > 0).astype(int), np.arange(len(col))
    return -(bias[:, k, side] + slope[:, k, side] * cross / 60)
'''
LINE_CAL_FROM_EXPERT = '''
rows = {(c, p): i for i, (c, p) in enumerate(zip(expert.cycle.values, expert.pass_id.values))}


def calibration(times, row, col):
    """The Unsmoothed store has no calibration yet: the Expert's (experimental v0.1) of each passage with the same
    cycle and pass, interpolated along track, at each pixel's distance to nadir (its line was moved by whole 250 m
    columns: cross_shift), subtracted."""
    along = xr.DataArray(sub.along.values[row], dims="pixel")
    near = expert[["cal_bias", "cal_slope"]].sel(along=slice(float(along.min()) - 4, float(along.max()) + 4))
    shift = np.nan_to_num(sub.cross_shift.isel(time=times, along=row).values)  # (passages, pixels)
    out = []
    for k, i in enumerate(times):
        j = rows.get((int(sub.cycle.values[i]), int(sub.pass_id.values[i])))
        if j is None:  # the Expert has no such passage
            out.append(np.full(len(col), np.nan))
            continue
        e = near.isel(time=j).interp(along=along, kwargs={"fill_value": None})
        b = sub.cross.values[col] - 0.25 * shift[k]
        side, n = (b > 0).astype(int), np.arange(len(col))
        out.append(-(e.cal_bias.values[n, side] + e.cal_slope.values[n, side] * b / 60))
    return np.array(out)
'''


def line_snippet(points, names, step=2.0, start=None, end=None, mean="time", label="", target=STORE, radius=1.5,
                 mask=None, db=False, units="m", dtu=None, cal=None):
    """The code for the values along a polyline: every `step` km along great circles, each passage's nearest
    pixel within `radius` km (its pass's grid is the same every cycle; with `mask`, a quality flag, only where it is 0). With `start`/`end`, the profile of the passages
    in those dates, one curve each; without, the Hovmöller of the whole mission (minus the mean over time at each
    point if `mean` is "time"). `dtu`: plus the correction to the DTU25 mean sea surface, `cal`: minus the
    calibration; each the store's own ("here") or interpolated from another one's (its target: the Expert, for the
    Unsmoothed)."""
    expert = next((t for t in (dtu, cal) if t not in (None, "here")), None)
    corrections = ((LINE_EXPERT.format(repo=repository(expert)[2]) if expert else "")
                   + ({None: "", "here": LINE_DTU25_HERE}.get(dtu, LINE_DTU25_FROM_EXPERT))
                   + ({None: "", "here": LINE_CAL_HERE}.get(cal, LINE_CAL_FROM_EXPERT)))
    value = f"sum(sub[n].isel(pick).values for n in {list(names)})"
    used = ["dtu25"] * bool(dtu) + ["calibration"] * bool(cal)
    if used:  # the corrections at the pixels, (passages, pixels)
        value = f"({value} + sum(c(times, row, col) for c in [{', '.join(used)}]).ravel())"
    window = f'''sub = sub.isel(time=((sub.time >= np.datetime64("{start}")) & (sub.time <= np.datetime64("{end}"))).values)
''' if start else ""
    if start:
        figure = '''plt.figure(figsize=(9, 4))
for (p, c, t), v in zip(passages, values):  # one curve per passage
    plt.plot(distance, v, label=f"pass {p}, cycle {c}, {str(t)[:16]}")
plt.legend(fontsize=7)
plt.ylabel("{units}")'''
    else:
        figure = ('''values = values - np.nanmean(values, axis=0)  # minus the mean over time at each point
''' if mean == "time" else "") + '''plt.figure(figsize=(9, 6))
times = np.array([t for *_, t in passages])
k, j = np.nonzero(np.isfinite(values))  # a small square per value: each passage at its own time
lim = np.nanpercentile(abs(values), 98)
plt.scatter(distance[j], times[k], c=values[k, j], s=3, marker="s", linewidths=0, cmap="RdBu_r",
            vmin=-lim if np.nanmin(values) < 0 else np.nanpercentile(values, 2), vmax=lim)
plt.colorbar(label="{units}")'''
    figure = figure.replace("{units}", units)
    vertices = ", ".join(f"({x:.4f}, {y:.4f})" for x, y in points)
    return f'''{(DTU25_CREDIT if dtu else "") + opening(target, "import numpy as np\nimport matplotlib.pyplot as plt\nfrom scipy.spatial import cKDTree\n")}
points, step = [{vertices}], {step:g}  # (lon, lat) vertices, km between samples
R = 6371.0
unit = lambda lon, lat: np.stack([np.cos(np.deg2rad(lat)) * np.cos(np.deg2rad(lon)),
                                  np.cos(np.deg2rad(lat)) * np.sin(np.deg2rad(lon)), np.sin(np.deg2rad(lat))], -1)
# the samples: every `step` km along the great circles between the vertices
v = unit(*np.array(points).T)
samples = [v[:1]]
for a, b in zip(v[:-1], v[1:]):
    angle = np.arccos(np.clip(a @ b, -1, 1))
    if angle:  # a repeated vertex adds nothing
        f = np.r_[np.arange(step / R, angle, step / R), angle][:, None]
        samples.append((np.sin(angle - f) * a + np.sin(f) * b) / np.sin(angle))
samples = np.concatenate(samples)
distance = np.cumsum(np.r_[0, R * np.arccos(np.clip((samples[:-1] * samples[1:]).sum(1), -1, 1))])
lat_s = np.rad2deg(np.arcsin(samples[:, 2]))

# the passes over the line's box (with a swath's half width around it), the lines near it
lon_s = np.rad2deg(np.arctan2(samples[:, 1], samples[:, 0]))
west, east = np.unwrap(lon_s, period=360).min() - 1, np.unwrap(lon_s, period=360).max() + 1
sub = ds.sel(lat=slice(lat_s.min() - 2, lat_s.max() + 2))
first = (sub.lon_min - west) % 360
hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)
{window}{corrections}
passages, values = [], []
for p in np.unique(sub.pass_id):
    times = np.flatnonzero(sub.pass_id.values == p)
    geo = sub[["latitude", "longitude"]].isel(time=times[0]).load()  # the pass's grid, the same every cycle
    ok = np.isfinite(geo.latitude.values) & np.isfinite(geo.longitude.values)
    d, j = cKDTree(unit(geo.longitude.values[ok], geo.latitude.values[ok])).query(samples, distance_upper_bound={radius:g} / R)
    hit = np.flatnonzero(np.isfinite(d))
    if not len(hit):
        continue
    row, col = np.unravel_index(np.flatnonzero(ok)[j[hit]], ok.shape)
    pick = dict(time=xr.DataArray(np.repeat(times, len(hit)), dims="pixel"),  # every passage at those pixels
                along=xr.DataArray(np.tile(row, len(times)), dims="pixel"),
                cross=xr.DataArray(np.tile(col, len(times)), dims="pixel"))
    v = {value}{f""" * np.where(sub["{mask}"].isel(pick).values == 0, 1, np.nan)""" if mask else ""}
{"    v = 10 * np.log10(np.where(v > 0, v, np.nan))  # in dB" + chr(10) if db else ""}    v = v.reshape(len(times), len(hit))
    for k, i in enumerate(times):
        if np.isfinite(v[k]).any():
            line = np.full(len(samples), np.nan)
            line[hit] = v[k]
            passages.append((int(p), int(sub.cycle.values[i]), sub.time.values[i]))
            values.append(line)
values = np.array(values)  # (passages, samples)

{figure}
plt.xlabel("km along the line")
plt.title("{label}")
plt.show()
'''
