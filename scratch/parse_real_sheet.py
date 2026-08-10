import sys
import csv
import httpx

url = "https://docs.google.com/spreadsheets/d/1tMTtgF1nbWG_67FKEjXJJ4Cxqk0zEKHKuao1irPtyis/gviz/tq?tqx=out:csv&gid=988642600"
resp = httpx.get(url)

print("STATUS CODE:", resp.status_code)
lines = resp.text.splitlines()
print("TOTAL ROWS:", len(lines))

reader = csv.reader(lines)
rows = list(reader)

print("\n--- HEADER ROW ---")
if rows:
    print(rows[0])

print("\n--- SEARCHING FOR 'Steel Ball Run' or 'Vinland Saga' or 'JoJo' ---")
found = []
for idx, r in enumerate(rows):
    row_str = " ".join(r).lower()
    if "steel ball" in row_str or "vinland" in row_str or "jojo" in row_str or "sample manga" in row_str:
        found.append((idx, r))

print(f"FOUND {len(found)} MATCHING ROWS:")
for idx, r in found[:10]:
    print(f"Row {idx}: {r}")
