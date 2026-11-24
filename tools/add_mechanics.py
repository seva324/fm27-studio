import json
from pathlib import Path

path = Path(__file__).resolve().parent.parent / "mechanics.json"
data = json.loads(path.read_text(encoding="utf-8"))
values = data["values"]
have = {v["name"] for v in values}

new = [
    {
        "name": "CA share in ratings",
        "group": "rating",
        "value": 0.5,
        "unit": "share",
        "source": ("0x14074042C mov rax,0x3FE0000000000000 and 0x140740436 mov [rbx+0x38],rax; "
                   "the field is CaSharePercent; english.json CaShare.Help says 'Default: 50%'"),
    },
    {
        "name": "rating formula",
        "group": "rating",
        "value": "CA_SHARE * CA / 200 + (1 - CA_SHARE) * weighted role score",
        "unit": "ratio",
        "source": ("english.json CaShare.Help: 'How much CA counts in Actual Rating and "
                   "Potential Rating. The rest comes from the player's best role. Default: 50%.'; "
                   "CA_SHARE at 0x14074042C; role weights at 0x140740455/0x1407403FB"),
    },
    {
        "name": "position penalty ladder length",
        "group": "rating",
        "value": 20,
        "unit": "entries",
        "source": "0x1407404C0 cmp dword ptr [rcx+8],0x14; 0x140741F50 allocates 0x14 ints",
    },
    {
        "name": "role weight group total",
        "group": "rating",
        "value": 100,
        "unit": "percent",
        "source": "0x140741DE0 requires each of the three group totals to equal 0x64 (100)",
    },
]

for entry in new:
    if entry["name"] not in have:
        values.append(entry)

path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("values now", len(values))
