"""Minimal JSON GET with retries. Errors never include the URL, so API keys cannot leak into logs."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


def get_json(url, params=None, timeout=15, retries=2, headers=None, sleep=time.sleep):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ironbull-bias/0.2", **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                sleep(2 * (attempt + 1))
            elif 400 <= e.code < 500:
                break
        except Exception as e:  # network, timeout, bad JSON
            last = f"{type(e).__name__}"
            sleep(1)
    return None, last
