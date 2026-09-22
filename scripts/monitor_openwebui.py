"""一次性监控 + 验证 open-webui 容器：下载进度 -> 应用启动 -> 登录页可用。

用法（需要一次性提权，因为沙箱禁止 docker 访问命名管道）：
    python monitor_openwebui.py

退出码：0 = 已健康并验证通过；2 = 超时未就绪（附带诊断）；3 = 容器不在运行。
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:3000"
CONTAINER = "open-webui"
DEADLINE_SECONDS = 2400
REPORT_EVERY = 60


def sh(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return "EXEC ERR %s" % e


def http(path, timeout=8):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def cache_size():
    out = sh(["docker", "exec", CONTAINER, "sh", "-c", "du -sh /app/backend/data/cache 2>/dev/null"])
    return out.split("\t")[0].strip() or "?"


def container_state():
    return sh(["docker", "inspect", CONTAINER, "--format",
               "{{.State.Status}}/{{.State.Health.Status}}/restarts={{.RestartCount}}"])


def report(tag, extra=""):
    print("[%s] %s | cache=%s %s" % (tag, container_state(), cache_size(), extra), flush=True)


start = time.time()
last_report = 0.0
status = None
healthy = False

while time.time() - start < DEADLINE_SECONDS:
    try:
        code, _ = http("/health", timeout=6)
        status = code
        if code == 200:
            healthy = True
            break
    except Exception as e:
        status = "ERR %s" % type(e).__name__

    if sh(["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER], timeout=30) != "running":
        print("FATAL: container not running", flush=True)
        print(sh(["docker", "logs", "--tail", "40", CONTAINER]), flush=True)
        sys.exit(3)

    now = time.time()
    if now - last_report >= REPORT_EVERY:
        last_report = now
        report("%4ds" % int(now - start), "http=%s" % status)
    time.sleep(20)

elapsed = int(time.time() - start)

if not healthy:
    print("TIMEOUT after %ds: http=%s" % (elapsed, status), flush=True)
    report("final")
    print("--- last 15 log lines ---", flush=True)
    print(sh(["docker", "logs", "--tail", "15", CONTAINER]), flush=True)
    sys.exit(2)

print("HEALTHY after %ds" % elapsed, flush=True)
report("healthy")

try:
    code, body = http("/api/config", timeout=20)
    cfg = json.loads(body.decode("utf-8", "replace"))
    feats = cfg.get("features", {}) or {}
    print("  /health      = %s" % code, flush=True)
    print("  version      = %s" % cfg.get("version"), flush=True)
    print("  onboarding   = %s" % cfg.get("onboarding"), flush=True)
    print("  enable_signup= %s" % feats.get("enable_signup"), flush=True)
    print("  auth         = %s" % feats.get("auth"), flush=True)
except Exception as e:
    print("  /api/config ERR: %s %s" % (type(e).__name__, str(e)[:200]), flush=True)

try:
    code, body = http("/", timeout=20)
    text = body.decode("utf-8", "replace")
    print("  GET /        = %s, %d bytes, has <title>=%s"
          % (code, len(body), "<title>" in text), flush=True)
except Exception as e:
    print("  GET / ERR: %s %s" % (type(e).__name__, str(e)[:200]), flush=True)