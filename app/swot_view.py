"""Data side of the viewer: the plain xarray recipes from the store docstring, nothing else.
It needs only icechunk + xarray (and arraylake for a store on Earthmover), like any reader, so `snippet`
can show the same code to users."""
import os

import icechunk
import numpy as np
import xarray as xr

STORE = os.environ.get("SWOT_STORE", "s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2")
EARTH_RADIUS = 6371.0  # km


def open_store(target=STORE, branch="main"):
    """arraylake:ORG/REPO (Earthmover; the client gets the storage credentials from an Arraylake token),
    s3://bucket/prefix (credentials and region from AWS_* in the environment) or a local path."""
    if target.startswith("arraylake:"):
        import arraylake

        repo = arraylake.Client().get_repo(target.removeprefix("arraylake:"))
    elif target.startswith("s3://"):
        bucket, prefix = target.removeprefix("s3://").split("/", 1)
        repo = icechunk.Repository.open(icechunk.s3_storage(bucket=bucket, prefix=prefix, from_env=True))
    else:
        repo = icechunk.Repository.open(icechunk.local_filesystem_storage(target))
    return xr.open_zarr(repo.readonly_session(branch).store, consolidated=False, chunks=None).set_xindex("lat")


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


def point_series(ds, lon, lat, var="ssha_filtered", radius=5.0, min_pixels=None):
    """Mean of `var` over the pixels within `radius` km of (lon, lat), one value per passage.
    Passages with fewer than `min_pixels` valid pixels there (nadir gap, swath edge) are dropped: by default a
    quarter of what the circle holds on the 2 km grid, at least 1 (5 km: 5; 2 km: 1)."""
    if min_pixels is None:
        min_pixels = max(1, round(np.pi * radius**2 / 4 / 4))
    km_per_deg = EARTH_RADIUS * np.pi / 180
    dlon = radius / (km_per_deg * np.cos(np.deg2rad(lat)))
    # lat(along) is the nadir latitude: swath edges lean up to ~0.3 deg away from it
    dlat = radius / km_per_deg + 0.5
    sub = subset(ds, lon - dlon, lon + dlon, lat - dlat, lat + dlat)[[var, "latitude", "longitude"]].load()
    near = sub[var].where(distance(sub.latitude, sub.longitude, lat, lon) <= radius)
    count = near.count(("along", "cross"))
    out = xr.Dataset({var: near.mean(("along", "cross")), "pixels": count})
    return out.isel(time=(count >= min_pixels).values)


def opening(target=STORE, imports=""):
    """The first lines of every code snippet: the imports, then open the repository and the dataset."""
    if target.startswith("arraylake:"):
        library, how = "arraylake", "an Arraylake token: run `arraylake auth login` once"
        repo = f'arraylake.Client().get_repo("{target.removeprefix("arraylake:")}")'
    else:
        bucket, prefix = target.removeprefix("s3://").split("/", 1)
        library = "icechunk"
        how = "AWS credentials from the environment (AWS_PROFILE or AWS_ACCESS_KEY_ID...), region us-east-1"
        repo = f'icechunk.Repository.open(icechunk.s3_storage(bucket="{bucket}", prefix="{prefix}", from_env=True))'
    return f'''import {library}
import xarray as xr
{imports}
# {how}
repo = {repo}
ds = xr.open_zarr(repo.readonly_session("main").store, consolidated=False, chunks=None).set_xindex("lat")
'''


def snippet(west, east, south, north, start, end, var="ssha_filtered", target=STORE):
    """The code a user runs to get the same subset, with icechunk + xarray only. The longitude filter and
    the latitude cut are left out when they keep everything (west == east, e.g. -180 to 180, is all longitudes)."""
    # the generated code is text at column 0
    lats = "" if south <= -90 and north >= 90 else f", lat=slice({south:g}, {north:g})"
    lons = "" if (east - west) % 360 == 0 else f'''west, east = {west:g}, {east:g}
first = (sub.lon_min - west) % 360  # how far east of `west` each line starts
hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)
'''
    return f'''{opening(target)}
sub = ds.sel(time=slice("{start}", "{end}"){lats})
{lons}sub = sub[["{var}"]].load()  # dims (time, along, cross); latitude/longitude come along
'''


def series_snippet(lon, lat, names, radius=5.0, label="", target=STORE):
    """The code for the time series at a point (as `point_series`), with a figure."""
    value = " + ".join(f'sub["{n}"]' for n in names)
    return f'''{opening(target, "import numpy as np\nimport matplotlib.pyplot as plt\n")}
lon, lat, radius = {lon:.4f}, {lat:.4f}, {radius:g}  # km
# every passage over the point (the lines near it), then the mean of its pixels within `radius`
km = 6371.0 * np.pi / 180
west, east = lon - radius / (km * np.cos(np.deg2rad(lat))), lon + radius / (km * np.cos(np.deg2rad(lat)))
sub = ds.sel(lat=slice(lat - radius / km - 0.5, lat + radius / km + 0.5))  # swath edges lean ~0.3 deg off nadir
first = (sub.lon_min - west) % 360
hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)[{list(names) + ["latitude", "longitude"]}].load()
la, lo = np.deg2rad(sub.latitude), np.deg2rad(sub.longitude - lon)
d = 2 * 6371.0 * np.arcsin(np.sqrt(np.sin((la - np.deg2rad(lat)) / 2) ** 2
                                   + np.cos(la) * np.cos(np.deg2rad(lat)) * np.sin(lo / 2) ** 2))
near = ({value}).where(d <= radius)
count = near.count(("along", "cross"))
# passages with too few pixels there (swath edge, nadir gap) are left out
series = near.mean(("along", "cross")).where(count >= max(1, round(np.pi * radius**2 / 16))).dropna("time")

plt.figure(figsize=(9, 4))
series.plot(marker=".")
plt.title("{label}mean within {radius:g} km of {lat:.2f}, {lon:.2f}")
plt.ylabel("m")
plt.show()
'''


def line_snippet(points, names, step=2.0, start=None, end=None, mean="time", label="", target=STORE):
    """The code for the values along a polyline: every `step` km along great circles, each passage's nearest
    pixel within 1.5 km (its pass's grid is the same every cycle). With `start`/`end`, the profile of the passages
    in those dates, one curve each; without, the Hovmöller of the whole mission (minus the mean over time at each
    point if `mean` is "time")."""
    value = " + ".join(f'sub["{n}"]' for n in names)
    window = f'''sub = sub.isel(time=((sub.time >= np.datetime64("{start}")) & (sub.time <= np.datetime64("{end}"))).values)
''' if start else ""
    if start:
        figure = '''plt.figure(figsize=(9, 4))
for (p, c, t), v in zip(passages, values):  # one curve per passage
    plt.plot(distance, v, label=f"pass {p}, cycle {c}, {str(t)[:16]}")
plt.legend(fontsize=7)
plt.ylabel("m")'''
    else:
        figure = ('''values = values - np.nanmean(values, axis=0)  # minus the mean over time at each point
''' if mean == "time" else "") + '''plt.figure(figsize=(9, 6))
times = np.array([t for *_, t in passages])
k, j = np.nonzero(np.isfinite(values))  # a small square per value: each passage at its own time
lim = np.nanpercentile(abs(values), 98)
plt.scatter(distance[j], times[k], c=values[k, j], s=3, marker="s", linewidths=0, cmap="RdBu_r",
            vmin=-lim if np.nanmin(values) < 0 else np.nanpercentile(values, 2), vmax=lim)
plt.colorbar(label="m")'''
    vertices = ", ".join(f"({x:.4f}, {y:.4f})" for x, y in points)
    return f'''{opening(target, "import numpy as np\nimport matplotlib.pyplot as plt\nfrom scipy.spatial import cKDTree\n")}
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
{window}
passages, values = [], []
for p in np.unique(sub.pass_id):
    times = np.flatnonzero(sub.pass_id.values == p)
    geo = sub[["latitude", "longitude"]].isel(time=times[0]).load()  # the pass's grid, the same every cycle
    ok = np.isfinite(geo.latitude.values) & np.isfinite(geo.longitude.values)
    d, j = cKDTree(unit(geo.longitude.values[ok], geo.latitude.values[ok])).query(samples, distance_upper_bound=1.5 / R)
    hit = np.flatnonzero(np.isfinite(d))
    if not len(hit):
        continue
    row, col = np.unravel_index(np.flatnonzero(ok)[j[hit]], ok.shape)
    pick = dict(time=xr.DataArray(np.repeat(times, len(hit)), dims="pixel"),  # every passage at those pixels
                along=xr.DataArray(np.tile(row, len(times)), dims="pixel"),
                cross=xr.DataArray(np.tile(col, len(times)), dims="pixel"))
    v = sum(sub[n].isel(pick).values for n in {list(names)}).reshape(len(times), len(hit))
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
