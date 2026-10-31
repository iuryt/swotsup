"""The daily maps of the whole globe (#98), built by swot_cloud (`swot_cloud.maps`, in a repository of their own): each
day's L2 Expert passages in Web Mercator rasters of zooms 0-4, so that any window of days is the sum of its days.

Per zoom z (group `z{z}`, 256 * 2**z pixels a side, rows from the south; zooms 0-4 in swot_cloud's), arrays (time, y, x), a day from DAY0 each:
`count` (2 km open-ocean cells in the pixel that day) and `ssha` (their mean, int16 mm: SSHA with the crossover
correction, DTU25 and the calibration, without the cells the edit flag removes), and the same for continental water
(`count_continental_water`, `ssha_continental_water`). The root's `source_snapshot` names the Expert snapshot each day
was built from ("" = not built). A window's mean is sum(count x ssha) / sum(count).
"""
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import zarr

import swot_view

DAY0 = np.datetime64("2023-01-01")
SURFACES = ("", "_continental_water")  # open ocean; and continental water, kept with it if chosen


def coarser(a):
    """The next zoom out: each 2 x 2 block summed (sums and counts add up)."""
    n = a.shape[-1] // 2
    return a.reshape(a.shape[:-2] + (n, 2, n, 2)).sum((-3, -1))


class Rasters:
    """The maps at the repository's latest commit (pinned: restart for a newer one)."""

    def __init__(self, target):
        repo = swot_view.open_repo(target)
        self.commit = next(iter(repo.ancestry(branch="main")))
        self.root = zarr.open_group(repo.readonly_session(snapshot_id=self.commit.id).store, mode="r")
        self.zoom = max(self.root.attrs["zooms"])  # the finest
        built = np.flatnonzero(self.root["source_snapshot"][:] != "")
        self.days = set(built.tolist())
        self.first = DAY0 + np.timedelta64(int(built.min()), "D") if len(built) else None
        self.last = DAY0 + np.timedelta64(int(built.max()), "D") if len(built) else None

    def covers(self, start, days):
        """Whether every day of a window is built."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        return all(d in self.days for d in range(k, k + days))

    def sums(self, z, sel, water):
        """The latest day with data at each pixel of the selection's days (a day's own passages averaged: its
        crossovers): its count x ssha (m) and its count, float64 (the open ocean, and continental water if chosen)."""
        total = n = 0
        for s in SURFACES if water else SURFACES[:1]:
            count = self.root[f"z{z}/count{s}"].get_orthogonal_selection(sel).astype("float64")
            a = self.root[f"z{z}/ssha{s}"]
            raw = a.get_orthogonal_selection(sel)
            value = np.where(raw == a.fill_value, 0.0, raw * a.attrs.get("scale_factor", 1.0))  # count is 0 there
            total, n = total + value * count, n + count  # (days, ...)
        # each pixel's latest day with any count (none: day 0, count 0)
        last = n.shape[0] - 1 - np.argmax(n[::-1] > 0, axis=0)
        return np.take_along_axis(total, last[None], 0)[0], np.take_along_axis(n, last[None], 0)[0]

    def mean(self, z, rows, cols, start, days, water=False, counts=False):
        """The window's mean on a block of zoom z's raster (rows from the south), NaN where nothing (`counts`: and how
        many 2 km cells each pixel has)."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        total, n = self.sums(z, (slice(k, k + days), rows, cols), water)
        with np.errstate(invalid="ignore", divide="ignore"):
            m = np.where(n > 0, total / n, np.nan)
        return (m, n) if counts else m

    def pyramid(self, start, days, water=False):
        """The window's means and counts at every zoom ({z: ((size, size), (size, size)) float32, rows from the south}),
        from the finest zoom's days, a few read at once (each day whole: one object, its shard): at each pixel its
        latest day with data, at each zoom (a coarser pixel: the latest day with data in its block, as `mean`)."""
        k = int((np.datetime64(start) - DAY0) / np.timedelta64(1, "D"))
        total, n = {}, {}
        with ThreadPoolExecutor(4) as pool:  # ~64 MB a day and surface in memory at once
            for t, c in pool.map(lambda d: self.sums(self.zoom, (slice(d, d + 1), slice(None), slice(None)), water),
                                 range(k, k + days)):
                for z in range(self.zoom, -1, -1):  # each zoom: this day's blocks cover the earlier days' (as `mean`)
                    if z in total:
                        total[z], n[z] = np.where(c > 0, t, total[z]), np.where(c > 0, c, n[z])
                    else:
                        total[z], n[z] = t, c
                    if z:
                        t, c = coarser(t), coarser(c)
        with np.errstate(invalid="ignore", divide="ignore"):
            return {z: (np.where(n[z] > 0, total[z] / n[z], np.nan).astype("float32"), n[z].astype("float32"))
                    for z in total}
