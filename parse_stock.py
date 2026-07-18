import re
import json

with open("xkom_page.html", "r", encoding="utf-8") as f:
    html = f.read()

# Let's find all occurrences of JSON-like structures that have departmentHeader
# e.g. {"departmentHeader":{"id":"...","name":"..."},"availableCount":...,"availabilityText":"...","deliveryText":"..."}
pattern = r'\{"departmentHeader":\{"id":"\d+","name":"[^"]+"\},"availableCount":\d+,"availabilityText":"[^"]*","deliveryText":"[^"]*"\}'
matches = re.findall(pattern, html)

if not matches:
    # Try a looser pattern
    pattern = r'\{"departmentHeader":\{[^\}]+\},[^\}]+}'
    matches = re.findall(pattern, html)

print(f"Znaleziono {len(matches)} rekordów dostępności w salonach:")
for m in matches:
    try:
        data = json.loads(m)
        name = data["departmentHeader"]["name"]
        count = data.get("availableCount", 0)
        text = data.get("availabilityText", "")
        delivery = data.get("deliveryText", "")
        if "Warszawa" in name:
            print(f"- {name}: Ilość: {count} | {text} | {delivery}")
    except Exception as e:
        pass
