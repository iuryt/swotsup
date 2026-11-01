# Anonymous usage data

**Purpose:** to understand how SWOT's Up is used (which regions, periods, variables and scales people look at, which
features they use, from which countries, and how fast the server answers), to run and improve the tool and to report
its use. It is operational data, not collected for research. A future research use of these anonymous data would be
a secondary analysis, submitted to the University of Delaware IRB first.

**Collection starts** with release 0.13.0 on the public server (the EC2 behind the Lambda link), October 2026.

**What visitors are told** (About): "To understand how the explorer is used, we record anonymous usage: what is viewed,
and the country the connection comes from, found from its address, which we do not keep. No cookies and no personal
information."

**Kept** (a file per day, one JSON row per line):
- `events-YYYY-MM-DD.jsonl`: `time`, `session` (random, this browser tab only, nothing stored in the browser),
  `visitor_day` (a hash of the address and browser with a salt that changes every day and is never kept: unique
  visitors per day; no way back to the address, no linking across days), `country` (DB-IP Lite, CC BY 4.0), `screen`
  (desktop or phone), `version`, `event` (open, view, point, series, line, zoom_250m, code, download, link, close)
  and the view on screen (`west east south north start days variable lon lat zoom projection mode cmap`), with what
  each event adds (`from_link`, `point_lon point_lat`, `line line_km line_kind step`, `what`).
- `requests-YYYY-MM-DD.jsonl`: `time`, `endpoint` (the route, not the values asked), `seconds`, `status`, `sample`
  (tile requests: one in 10 is kept).

- `health-YYYY-MM-DD.jsonl`: the machine's numbers every minute (`cores` busy, memory, network, requests in flight,
  busy threads, views being read, the Lambda function's answers and failures): nothing about visitors.
- `server-log.txt`: the server's last messages (errors, restarts), without its access lines (no addresses).

**Never kept:** the address, a hash of it that lasts, browser or system details, anything typed, any identifier lasting
more than a day.

**Where:** on the instance's disk (`/opt/telemetry`), copied every 10 minutes and before it stops to
`s3://iuryt-shared-east/telemetry/swot-explorer/` (the instance may only write there).

**Reading them:**

```python
import duckdb
duckdb.sql("""SELECT variable, count(DISTINCT session) AS sessions
              FROM read_json_auto('s3://iuryt-shared-east/telemetry/swot-explorer/events-*.jsonl')
              GROUP BY variable ORDER BY sessions DESC""").show()
```
or `pd.concat(pd.read_json(f, lines=True) for f in files)`.
