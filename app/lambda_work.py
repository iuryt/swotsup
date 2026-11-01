"""The server's heavy requests as an AWS Lambda function (SWOT_LAMBDA): the server's own code, without its opening
view (SWOT_ROLE=lambda), so each runs on its own machine however many visitors ask at once. The Arraylake token is
read once per machine from SSM (SWOT_TOKEN_PARAMETER). Event: {kind: "series", lon, lat, variable, radius, fine};
returns what the endpoint does, or {error}."""
import os

os.environ.setdefault("SWOT_ROLE", "lambda")
os.environ.setdefault("HOME", "/tmp")  # the arraylake client keeps its settings under HOME; Lambda writes only /tmp
if "ARRAYLAKE_TOKEN" not in os.environ and os.environ.get("SWOT_TOKEN_PARAMETER"):
    import boto3
    os.environ["ARRAYLAKE_TOKEN"] = boto3.client("ssm").get_parameter(
        Name=os.environ["SWOT_TOKEN_PARAMETER"], WithDecryption=True)["Parameter"]["Value"]

import server  # noqa: E402 (opens the stores: once per machine, at its first request)


def handler(event, context):
    try:
        if event.get("kind", "series") == "series":
            return server.series_here(float(event["lon"]), float(event["lat"]), event["variable"],
                                      float(event.get("radius", 5)), bool(event.get("fine", False)))
        return {"error": f"unknown kind {event.get('kind')}"}
    except Exception as e:  # noqa: BLE001 (the server falls back to its own)
        return {"error": f"{type(e).__name__}: {e}"}
