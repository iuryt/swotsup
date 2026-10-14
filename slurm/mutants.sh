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
