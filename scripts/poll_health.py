import json
import time
import urllib.request

BASE = "http://127.0.0.1:3000"
DEADLINE_SECONDS = 1500


def get(path, timeout=8):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


start = time.time()
status = None
while time.time() - start < DEADLINE_SECONDS:
    try:
        status, _ = get("/health", timeout=6)
        if status == 200:
            break
    except Exception as e:
        status = "ERR %s" % type(e).__name__
    time.sleep(15)

elapsed = int(time.time() - start)
print("RESULT health=%s after %ds" % (status, elapsed), flush=True)

if status == 200:
    try:
        _, body = get("/api/config", timeout=15)
        cfg = json.loads(body.decode("utf-8", "replace"))
        feats = cfg.get("features", {}) or {}
        print("  version       =", cfg.get("version"))
        print("  onboarding    =", cfg.get("onboarding"))
        print("  enable_signup =", feats.get("enable_signup"))
        print("  auth          =", feats.get("auth"))
    except Exception as e:
        print("  config ERR:", type(e).__name__, str(e)[:150])