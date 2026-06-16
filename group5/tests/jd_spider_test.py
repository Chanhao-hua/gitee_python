import requests
ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
r = requests.get("https://so.m.jd.com/ware/search.action",
                 params={"keyword": "手机", "page": 1},
                 headers={"User-Agent": ua}, timeout=15)
print(r.status_code, r.headers.get("content-type"))
print(r.text[:1000])
