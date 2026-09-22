"""验证 open-webui 已可用于登录（只读探测，不创建任何账号）。"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3000"


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "verify"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return r.status, body[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return "ERR", type(e).__name__


print("--- 期望 401：未带 token 访问受保护接口 ---")
for p in ("/api/v1/auths/", "/api/v1/models", "/api/v1/users/"):
    print("  %-20s -> %s %s" % (p, *call(p)))

print("--- 期望 400/401：错误口令登录（证明登录接口活着） ---")
print("  POST /api/v1/auths/signin ->", call("/api/v1/auths/signin", "POST",
                                           {"email": "nobody@example.com", "password": "x"}))

print("--- 期望 200：公开配置 ---")
code, body = call("/api/config")
print("  GET /api/config ->", code, body[:120])

print("--- 期望 200：单页应用 ---")
code, body = call("/")
html = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
print("  GET / ->", code, "| 含 root div:", 'id="app"' in html or "<div id=" in html)