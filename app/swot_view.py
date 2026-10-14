"""Data side of the viewer: the plain xarray recipes from the store docstring, nothing else.
It needs only icechunk + xarray, like any reader, so `snippet` can show the same code to users."""
import os

import icechunk
import numpy as np
import xarray as xr

STORE = os.environ.get("SWOT_STORE", "s3://iuryt-shared-east/icechunk/ocean/swot_l3_lr_ssh_basic_v3_r2")
EARTH_RADIUS = 6371.0  # km


def open_store(target=STORE, branch="main"):
    """s3://bucket/prefix (credentials and region from AWS_* in the environment) or a local path."""
    if target.startswith("s3://"):
        bucket, prefix = target.removeprefix("s3://").split("/", 1)
        storage = icechunk.s3_storage(bucket=bucket, prefix=prefix, from_env=True)
    else:
        storage = icechunk.local_filesystem_storage(target)
    store = icechunk.Repository.open(storage).readonly_session(branch).store
    return xr.open_zarr(store, consolidated=False, chunks=None).set_xindex("lat")


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


def point_series(ds, lon, lat, var="ssha_filtered", radius=5.0, min_pixels=5):
    """Mean of `var` over the pixels within `radius` km of (lon, lat), one value per passage.
    Passages with fewer than `min_pixels` valid pixels there (nadir gap, swath edge) are dropped."""
    km_per_deg = EARTH_RADIUS * np.pi / 180
    dlon = radius / (km_per_deg * np.cos(np.deg2rad(lat)))
    # lat(along) is the nadir latitude: swath edges lean up to ~0.3 deg away from it
    dlat = radius / km_per_deg + 0.5
    sub = subset(ds, lon - dlon, lon + dlon, lat - dlat, lat + dlat)[[var, "latitude", "longitude"]].load()
    near = sub[var].where(distance(sub.latitude, sub.longitude, lat, lon) <= radius)
    count = near.count(("along", "cross"))
    out = xr.Dataset({var: near.mean(("along", "cross")), "pixels": count})
    return out.isel(time=(count >= min_pixels).values)


def snippet(west, east, south, north, start, end, var="ssha_filtered", target=STORE):
    """The code a user runs to get the same subset, with icechunk + xarray only. The longitude filter and
    the latitude cut are left out when they keep everything (west == east, e.g. -180 to 180, is all longitudes)."""
    bucket, prefix = target.removeprefix("s3://").split("/", 1)
    lats = "" if south <= -90 and north >= 90 else f", lat=slice({south:g}, {north:g})"
    lons = "" if (east - west) % 360 == 0 else f'''west, east = {west:g}, {east:g}
first = (sub.lon_min - west) % 360  # how far east of `west` each line starts
hit = ((first <= (east - west) % 360) | (first + sub.lon_max - sub.lon_min >= 360)).any("along")
sub = sub.isel(time=sub.pass_id.isin(sub.pass_number[hit]).values)
'''
    return f'''import icechunk
import xarray as xr

# AWS credentials from the environment (AWS_PROFILE or AWS_ACCESS_KEY_ID...), region us-east-1
storage = icechunk.s3_storage(bucket="{bucket}", prefix="{prefix}", from_env=True)
store = icechunk.Repository.open(storage).readonly_session("main").store
ds = xr.open_zarr(store, consolidated=False, chunks=None).set_xindex("lat")

sub = ds.sel(time=slice("{start}", "{end}"){lats})
{lons}sub = sub[["{var}"]].load()  # dims (time, along, cross); latitude/longitude come along
'''
