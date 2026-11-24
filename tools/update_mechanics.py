"""Add the decompiled clamp and precision values to mechanics.json.

Run once; it rewrites the rating group with the values read out of the
original's machine code, so the mechanics check can verify them.
"""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "mechanics.json"

NEW = [
    {
        "name": "attribute clamp maximum",
        "group": "rating",
        "value": 20,
        "unit": "max",
        "source": "code: mov r8d,0x14 at 0x1411782B8, passed to the clamp helper "
                  "sub_1404446B0 (0x1404446B0) with minimum 0 (xor r9d,r9d)",
    },
    {
        "name": "ability clamp maximum",
        "group": "rating",
        "value": 200,
        "unit": "max",
        "source": "code: mov r8d,0xC8 at 0x1411D0A48, passed to sub_1404446B0",
    },
    {
        "name": "condition clamp maximum",
        "group": "rating",
        "value": 10000,
        "unit": "max",
        "source": "code: mov r8d,0x2710 at 0x14102D4E0, passed to sub_1404446B0; "
                  "same site clamps sharpness and reputation",
    },
    {
        "name": "rating display decimals",
        "group": "rating",
        "value": 2,
        "unit": "decimals",
        "source": "code: the original's header reads 77.49 for 124/160, which is "
                  "the float32 ratio 0.7749999761581421 truncated to 2 decimals",
    },
    {
        "name": "rating ratio precision",
        "group": "rating",
        "value": 0.7749999761581421,
        "unit": "float32",
        "source": "derived: 124/160 stored as a float32; x100 = 77.49999761581421, "
                  "truncated to 77.49 exactly as the original's header shows",
    },
    {
        "name": "jadedness clamp minimum",
        "group": "rating",
        "value": -10000,
        "unit": "min",
        "source": "Presets/Destroy.json writes 1000 and the save holds -125, so "
                  "jadedness is signed and is not clamped at 0",
    },
]

REPLACE = {
    "rating formula": {
        "name": "rating formula",
        "group": "rating",
        "value": "truncate(float32(keySum/(keyCount*20))*100, 2)",
        "unit": "percent",
        "source": "derived from Ratings/rolesAndDuties.json and verified against the "
                  "original's header (77.49%); only WideForward/InPossession key-only "
                  "reproduces 77.49 across every role and duty in the map",
    },
}


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    values = data["values"]
    for index, item in enumerate(values):
        if item["name"] in REPLACE:
            values[index] = REPLACE[item["name"]]
    existing = {item["name"] for item in values}
    for item in NEW:
        if item["name"] not in existing:
            values.append(item)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    print(f"values now: {len(values)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
