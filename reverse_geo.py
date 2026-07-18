import urllib.request
import json

lat = 52.228516
lon = 20.984475

url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
req = urllib.request.Request(
    url, 
    headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
)

try:
    with urllib.request.urlopen(req) as response:
        res = json.loads(response.read().decode('utf-8'))
        print(json.dumps(res, indent=2))
except Exception as e:
    print("Error:", e)
