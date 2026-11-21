import json
from cpbl_fetcher import fetch_box_live

res_json = fetch_box_live("2026", "A", "274")
bat_list = json.loads(res_json.get("BattingJson") or "[]")

for row in bat_list:
    if str(row.get("HitterName") or "").strip() == "陳真":
        for k, v in row.items():
            print(f"  {k}: {v}")
        print("---")