"""Validate mechanics.json: every value has a source or is marked estimated."""

import json
import os

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "mechanics.json")
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)

values = data["values"]
print("version:", data["version"])
print("values:", len(values))
missing = [v["name"] for v in values
           if not v.get("source") and not v.get("estimated")]
print("without a source or estimate:", missing)
estimated = [v["name"] for v in values if v.get("estimated")]
print("estimated:", estimated)
bad = [v["name"] for v in values
       if not isinstance(v.get("value"), (int, float, str, bool))]
print("bad value types:", bad)
assert not missing, missing
assert not bad, bad
print("OK")
