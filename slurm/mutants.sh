#!/bin/bash
# Apply one bug at a time and check that the tests catch it.
cd "${SLURM_SUBMIT_DIR:-$PWD}"
export PYTHONDONTWRITEBYTECODE=1  # a reverted mutant can keep its size and mtime, so a stale .pyc would be reused
mutate() {  # file, sed expression, label
    cp "$1" "$1.bak"; sed -i "$2" "$1"
    if cmp -s "$1" "$1.bak"; then echo "NOT APPLIED: $3"
    elif /home/iury/.pixi/bin/pixi run pytest -q -x tests >/dev/null 2>&1; then echo "SURVIVED: $3"
    else echo "caught: $3"; fi
    mv "$1.bak" "$1"
}
mutate app/swot_view.py 's/    return out.isel(time=(count >= min_pixels).values)/    return out/' "point series keeps passages with too few pixels"
mutate app/swot_view.py 's/    near = sub\[var\].where(distance(sub.latitude, sub.longitude, lat, lon) <= radius)/    near = sub[var]/' "point series averages the whole cut"
mutate app/swot_view.py 's/sel(time=slice("{start}", "{end}")/sel(time=slice("{start}", None)/' "snippet ignores the end date"
mutate app/swot_view.py 's/    width = (east - west) % 360 or 360  # west == east/    width = (east - west) % 360  # west == east/' "a -180 to 180 box is empty"
mutate app/server.py 's/Image.fromarray(rgba\[::-1\])/Image.fromarray(rgba)/' "tiles upside down"
mutate app/server.py 's/    x0, y1 = -WORLD \/ 2 + x \* size, WORLD \/ 2 - y \* size/    x0, y1 = -WORLD \/ 2 + x * size, WORLD \/ 2 - (y + 1) * size/' "tiles one row off"
mutate app/server.py 's/per_day = over.cycle.to_series().groupby(over.time.to_index().floor("D"))/per_day = index.cycle.to_series().groupby(index.time.to_index().floor("D"))/' "timeline ignores the box"
mutate app/server.py 's/    lon = np.unwrap(lon, period=360, axis=1)/    pass/' "swaths torn at 180"
mutate app/server.py 's/                rows = slice(a, b)/                rows = usable/' "lines across land joined into one mesh"
mutate app/server.py 's/    return load(west, east, south, north, start, int(np.clip(days, 1, MAX_DAYS)), variable)\[0\]/    return next(reversed(loaded.values()))/' "tiles drawn from whatever view is in memory"
mutate app/swot_view.py 's/    lons = "" if (east - west) % 360 == 0 else/    lons = "" if False else/' "global code keeps the longitude filter"
mutate app/server.py 's/str(pd.Timestamp(data.line_time\[i, line\]).round("s"))/str(pd.Timestamp(data.sub.time.values[i]).round("s"))/' "click gives the passage's mean time, not its line's"
mutate app/server.py 's/yield i, rows.start + block.start, /yield i, rows.start, /' "click takes the line from the wrong block"
mutate app/server.py 's/            if len(above) == 0 or len(below) == 0:/            if len(np.intersect1d(above, below)) == 0:/' "a click between two lines finds no data"
mutate app/server.py 's/    angles = np.arccos(np.clip((v\[:-1\] \* v\[1:\]).sum(-1), -1, 1))  # of each segment/    angles = np.deg2rad(np.hypot(np.diff(lon), np.diff(lat)))  # of each segment/' "transect segments measured in degrees, not along great circles"
mutate app/server.py 's/d, j = tree.query(pts, distance_upper_bound=radius \/ swot_view.EARTH_RADIUS)/d, j = tree.query(pts)/' "transect fills the nadir gap and beyond the swath"
mutate app/server.py 's/        t = pd.Timestamp(data.line_time\[i, seen\[len(seen) \/\/ 2\]\]).round("s")/        t = pd.Timestamp(data.sub.time.values[i]).round("s")/' "transect gives the passage's mean time"
mutate app/server.py 's/    dist = np.r_\[np.arange(0, length, step), length\]  # and the last vertex/    dist = np.arange(0, length, step)/' "transect stops short of its last vertex"
mutate app/server.py 's/        times = data.line_time\[i, rows\[i\].clip(0)\]/        times = data.line_time[i, np.full_like(rows[i], seen[len(seen) \/\/ 2])]/' "transect gives every point the time of the middle line"
mutate app/swot_view.py 's/repo = arraylake.Client().get_repo("{target.removeprefix("arraylake:")}")/repo = arraylake.Client().get_repo("{target}")/' "Arraylake code asks for the wrong repository name"
mutate app/swot_view.py 's/        min_pixels = max(1, round(np.pi \* radius\*\*2 \/ 4 \/ 4))/        min_pixels = 5/' "a 2 km time series drops every passage"
