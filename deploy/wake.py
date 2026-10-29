"""The explorer's link: an AWS Lambda function URL in front of the EC2 instance, which stops itself when idle
(deploy/boot.sh). A visit goes straight to the explorer when it answers; otherwise it starts the instance and
shows a page that waits for it (polling ?status) and then opens it. slurm/ec2.sh creates it, with HOST set.

With a domain (`slurm/ec2.sh domain`: CloudFront in front of the instance, failing over to this function when the
instance does not answer), FRONT is the domain's address: the function URL sends visits there, and what comes
through CloudFront (its header X-Swot-Front) gets the waiting page, which polls /api/info at the same address and
reloads when the instance answers, so the address people see never changes."""
import json
import os
import urllib.request

import boto3

ec2 = boto3.client("ec2")
HOST = os.environ["HOST"]  # <elastic ip>.sslip.io
NAME = os.environ.get("INSTANCE", "swot-explorer")
FRONT = os.environ.get("FRONT")  # https://<domain>/, once CloudFront is there

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SWOT's Up</title>
<style>
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0b1220; color: #dbe4f0;
         font: 16px/1.5 system-ui, sans-serif; }
  main { max-width: 30rem; padding: 2rem 1.5rem; text-align: center; }
  h1 { font-size: 1.25rem; margin: 0 0 .75rem; } p { margin: .5rem 0; color: #9fb0c6; }
  .spinner { width: 2.25rem; height: 2.25rem; margin: 0 auto 1.25rem; border-radius: 50%;
             border: 3px solid #23324a; border-top-color: #f28c28; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #state { font-size: .85rem; color: #6f819b; }
</style></head><body><main>
  <div class="spinner"></div>
  <h1>SWOT's Up is waking up</h1>
  <p>The server sleeps when nobody is using it, to save costs. Starting it takes about a minute;
     this page opens the explorer by itself when it is ready.</p>
  <p id="state">Starting the server&hellip;</p>
</main><script>
  const t0 = Date.now(), front = FRONT_PAGE;
  async function poll() {
    try {
      let s;
      if (front) {  // the same address: the instance answers it once up, this function (503) until then
        const r = await fetch("/api/info", { cache: "no-store" });
        if (r.ok) return location.reload();
        s = await r.json();
      } else {
        s = await (await fetch("?status=1", { cache: "no-store" })).json();
        if (s.ready) return location.replace(s.url + location.hash);  // a shared link's view comes along
      }
      const sec = Math.round((Date.now() - t0) / 1000);
      document.getElementById("state").textContent =
        (s.state === "running" ? "Server on, loading the data" : "Starting the server") + ` (${sec} s)`;
    } catch (e) {}
    setTimeout(poll, 4000);
  }
  poll();
</script></body></html>"""


def ready():
    try:
        with urllib.request.urlopen(f"https://{HOST}/api/info", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def wake():
    """Start the instance if it is stopped (one stopping is started by a later poll); its state."""
    found = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    instance = found["Reservations"][0]["Instances"][0]
    state = instance["State"]["Name"]
    if state == "stopped":
        ec2.start_instances(InstanceIds=[instance["InstanceId"]])
        state = "pending"
    return state


def handler(event, context):
    if FRONT and "x-swot-front" in (event.get("headers") or {}):  # through CloudFront: the instance did not answer
        state = wake()
        if event.get("rawPath", "/").startswith("/api/"):
            return {"statusCode": 503, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
                    "body": json.dumps(dict(ready=False, state=state))}
        return {"statusCode": 200, "headers": {"Content-Type": "text/html", "Cache-Control": "no-store"},
                "body": PAGE.replace("FRONT_PAGE", "true")}
    if FRONT:  # the function's own URL (links shared before the domain): to the domain, with what follows the #
        return {"statusCode": 302, "headers": {"Location": FRONT, "Cache-Control": "no-store"}}
    url = f"https://{HOST}/"
    up = ready()
    if "status" in (event.get("queryStringParameters") or {}):
        body = dict(ready=up, url=url, state="running" if up else wake())
        return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
                "body": json.dumps(body)}
    if up:
        return {"statusCode": 302, "headers": {"Location": url, "Cache-Control": "no-store"}}
    wake()
    return {"statusCode": 200, "headers": {"Content-Type": "text/html", "Cache-Control": "no-store"},
            "body": PAGE.replace("FRONT_PAGE", "false")}
