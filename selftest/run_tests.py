"""custom_pipes 集成自测：在 open-webui 容器内运行，用镜像自带的 httpx/pydantic 真实加载插件。

覆盖：
  - 两个插件文件的语法/编译
  - Pipe: pipes() / _endpoint() 校验 / 流式 happy path / 请求体与请求头 / 错误分支 / UserValves 默认值 / 脏 SSE 容错
  - Filter: inlet 注入与拦截 / stream 透传 / outlet
用法：python /tmp/run_tests.py [插件目录，默认 /tmp/selftest_pipes]
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIPES_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/selftest_pipes"
PORT = 18123
DEAD_PORT = 18124

RESULTS = []
RECORDS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print("%-4s %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "  <- " + str(detail)))


def load(path, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        RECORDS.append({"path": self.path, "headers": dict(self.headers), "body": raw})
        # 插件固定 POST 到 <base>/chat/completions，所以模拟服务只能按请求体里的 model 分流
        try:
            model = json.loads(raw.decode("utf-8")).get("model", "")
        except Exception:
            model = ""
        if model == "fail" or self.path.endswith("/fail"):
            payload = b'{"error":"boom"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if model == "badjson" or self.path.endswith("/badjson"):
            chunks = [
                "data: {oops}\n\n",
                'data: {"choices":[]}\n\n',
                'data: {"choices":[{"delta":{}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        else:
            chunks = [
                'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
        body = "".join(chunks).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def collect(gen):
    return "".join([chunk async for chunk in gen])


def main():
    print("== 环境 ==")
    print("python", sys.version.split()[0], "| cwd", os.getcwd())
    import httpx
    import pydantic

    print("httpx", httpx.__version__, "| pydantic", pydantic.VERSION)

    pipe_path = os.path.join(PIPES_DIR, "custom_model_pipe.py")
    filter_path = os.path.join(PIPES_DIR, "audit_filter.py")

    print("\n== T0 语法/编译 ==")
    for path in (pipe_path, filter_path):
        try:
            with open(path, encoding="utf-8") as fh:
                compile(fh.read(), path, "exec")
            check("compile %s" % os.path.basename(path), True)
        except SyntaxError as exc:
            check("compile %s" % os.path.basename(path), False, exc)

    # 清掉可能存在的旧 .pyc，避免用修改前的字节码跑测试
    cache = os.path.join(PIPES_DIR, "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)

    pipe_mod = load(pipe_path, "custom_model_pipe_under_test")
    filter_mod = load(filter_path, "audit_filter_under_test")

    print("\n== T1 pipes() 未配置 Key ==")
    p = pipe_mod.Pipe()
    out = p.pipes()
    check("未配置 api_key 时只暴露 error 项",
          isinstance(out, list) and len(out) == 1 and out[0]["id"] == "error", out)

    print("\n== T2 pipes() 解析 model_ids ==")
    p.valves.api_key = "sk-test"
    p.valves.model_ids = " deepseek-chat , deepseek-reasoner ,, "
    out = p.pipes()
    ids = [x["id"] for x in out]
    check("逗号/空格/空项处理正确", ids == ["deepseek-chat", "deepseek-reasoner"], ids)
    check("name 与 id 一致", all(x["id"] == x["name"] for x in out), out)

    print("\n== T3 _endpoint() 地址校验 ==")
    p.valves.api_base = "https://api.example.com/v1"
    check("https 正常拼接", p._endpoint() == "https://api.example.com/v1/chat/completions", p._endpoint())
    p.valves.api_base = "https://api.example.com/v1/"
    check("末尾斜杠被去掉", p._endpoint() == "https://api.example.com/v1/chat/completions", p._endpoint())
    p.valves.api_base = "http://127.0.0.1:11434/v1"
    check("默认禁止明文 http", _raises(ValueError, p._endpoint), "未抛异常")
    p.valves.allow_insecure_http = True
    check("allow_insecure_http 后放行", p._endpoint().startswith("http://127.0.0.1:11434"), p._endpoint())
    for bad, label in (
        ("ftp://x/v1", "非 http(s) 协议"),
        ("https://user:pw@api.example.com/v1", "含账号密码"),
        ("https://api.example.com/v1?k=1", "含查询参数"),
    ):
        p.valves.api_base = bad
        check("拒绝 %s" % label, _raises(ValueError, p._endpoint), "未抛异常")
    p.valves.api_base = "http://127.0.0.1:18123/v1"
    p.valves.allow_insecure_http = True

    print("\n== T4/T5 流式 happy path + 请求体/请求头 ==")
    events = []

    async def emitter(ev):
        events.append(ev)

    user = {"valves": pipe_mod.Pipe.UserValves(temperature=1.5, max_tokens=7)}
    body = {
        "model": "my_pipe.deepseek-chat",
        "messages": [{"role": "user", "content": "你好"}],
        "top_p": 0.9,
        "unknown_key": "should_be_dropped",
    }
    text = _run_pipe(p, body, user, emitter)
    check("流式内容拼接正确", text == "你好", repr(text))
    check("发出 status(开始) 与 status(完成) 两个事件",
          len(events) == 2 and events[0]["data"]["done"] is False and events[1]["data"]["done"] is True, events)
    check("开始事件里带真实模型名", "deepseek-chat" in events[0]["data"]["description"], events[0])
    rec = RECORDS[-1]
    sent = json.loads(rec["body"].decode("utf-8"))
    check("请求路径为 /v1/chat/completions", rec["path"] == "/v1/chat/completions", rec["path"])
    check("model 前缀已剥离", sent["model"] == "deepseek-chat", sent["model"])
    check("强制 stream=True", sent["stream"] is True, sent.get("stream"))
    check("UserValves 生效 (temperature/max_tokens)",
          sent["temperature"] == 1.5 and sent["max_tokens"] == 7, sent)
    check("透传白名单字段 top_p", sent.get("top_p") == 0.9, sent.get("top_p"))
    check("丢弃非白名单字段", "unknown_key" not in sent, sent)
    check("Authorization 头正确",
          rec["headers"].get("Authorization") == "Bearer sk-test", rec["headers"].get("Authorization"))
    check("Accept 为 text/event-stream",
          rec["headers"].get("Accept") == "text/event-stream", rec["headers"].get("Accept"))

    print("\n== T6 HTTP 500 分支 ==")
    events.clear()
    p2 = pipe_mod.Pipe()
    p2.valves.api_key = "sk-test"
    p2.valves.api_base = "http://127.0.0.1:18123/v1"
    p2.valves.allow_insecure_http = True
    text = _run_pipe(p2, {"model": "x.fail", "messages": [{"role": "user", "content": "hi"}]},
                     {"valves": pipe_mod.Pipe.UserValves()}, emitter)
    check("500 时给出中文错误", text == "API 请求失败（HTTP 500）", repr(text))
    check("500 时仍收尾（3 个事件）", len(events) == 2 and events[-1]["data"]["done"] is True, events)

    print("\n== T7 连接失败分支 ==")
    events.clear()
    p3 = pipe_mod.Pipe()
    p3.valves.api_key = "sk-test"
    p3.valves.api_base = "http://127.0.0.1:%d/v1" % DEAD_PORT
    p3.valves.allow_insecure_http = True
    text = _run_pipe(p3, {"model": "x.y", "messages": [{"role": "user", "content": "hi"}]},
                     {"valves": pipe_mod.Pipe.UserValves()}, emitter)
    check("端口不通时给出连接失败提示", "连接失败" in text, repr(text))
    check("连接失败也收尾", events[-1]["data"]["done"] is True, events)

    print("\n== T8/T9 前置校验（返回字符串而非生成器） ==")
    p4 = pipe_mod.Pipe()
    ret = asyncio.run(p4.pipe({"model": "x.y", "messages": [{"role": "user", "content": "hi"}]}, {}, __event_emitter__=None))
    check("缺少 api_key 返回提示字符串", isinstance(ret, str) and "API Key" in ret, repr(ret))
    p5 = pipe_mod.Pipe()
    p5.valves.api_key = "sk-test"
    ret = asyncio.run(p5.pipe({"model": "x.y", "messages": []}, {}, __event_emitter__=None))
    check("空 messages 返回提示字符串", isinstance(ret, str) and "缺少" in ret, repr(ret))
    ret = asyncio.run(p5.pipe({"model": "", "messages": [{"role": "user", "content": "hi"}]}, {}, __event_emitter__=None))
    check("空 model 返回提示字符串", isinstance(ret, str) and "缺少" in ret, repr(ret))

    print("\n== T10 无 __user__.valves 时用默认值 ==")
    p6 = pipe_mod.Pipe()
    p6.valves.api_key = "sk-test"
    p6.valves.api_base = "http://127.0.0.1:18123/v1"
    p6.valves.allow_insecure_http = True
    _run_pipe(p6, {"model": "x.y", "messages": [{"role": "user", "content": "hi"}]}, {}, emitter)
    sent = json.loads(RECORDS[-1]["body"].decode("utf-8"))
    check("默认 temperature=0.7 / max_tokens=2048",
          sent["temperature"] == 0.7 and sent["max_tokens"] == 2048, sent)

    print("\n== T11 脏 SSE 容错 ==")
    p7 = pipe_mod.Pipe()
    p7.valves.api_key = "sk-test"
    p7.valves.api_base = "http://127.0.0.1:18123/v1"
    p7.valves.allow_insecure_http = True
    text = _run_pipe(p7, {"model": "x.badjson", "messages": [{"role": "user", "content": "hi"}]},
                     {"valves": pipe_mod.Pipe.UserValves()}, emitter)
    check("坏 JSON / 空 choices / 无 content 均被跳过", text == "好", repr(text))

    print("\n== T12 Filter: inlet ==")
    f = filter_mod.Filter()
    f.valves.append_system_note = True
    body = {"messages": [{"role": "user", "content": "正常问题"}]}
    out = asyncio.run(f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=20000)}))
    check("inlet 在开头插入 system 提示",
          len(out["messages"]) == 2 and out["messages"][0]["role"] == "system", out["messages"])
    check("inlet 返回同一个 body 对象", out is body, "未原地修改")

    f.valves.append_system_note = False
    body = {"messages": [{"role": "user", "content": "正常问题"}]}
    out = asyncio.run(f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=20000)}))
    check("关闭注入后消息数不变", len(out["messages"]) == 1, out["messages"])

    print("\n== T13 Filter: 关键词拦截（含大小写归一） ==")
    f.valves.blocked_words = " 敏感词 , BAD "
    body = {"messages": [{"role": "user", "content": "这是 bad 内容"}]}
    check("大小写不敏感命中拦截",
          _raises(Exception, lambda: asyncio.run(
              f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=20000)}))),
          "未拦截")
    body = {"messages": [{"role": "user", "content": "这是无关内容"}]}
    check("未命中的词放行",
          asyncio.run(f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=20000)})) is body, "被误拦")

    print("\n== T14 Filter: 长度上限 ==")
    f.valves.blocked_words = ""
    body = {"messages": [{"role": "user", "content": "1234567890"}]}
    check("超过 max_length 被拦截",
          _raises(Exception, lambda: asyncio.run(
              f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=5)}))),
          "未拦截")
    body = {"messages": [{"role": "user", "content": "12345"}]}
    check("恰好等于 max_length 放行",
          asyncio.run(f.inlet(body, {"valves": filter_mod.Filter.UserValves(max_length=5)})) is body, "被误拦")

    print("\n== T15 Filter: stream / outlet ==")
    ev = {"type": "chat:completion", "data": {"choices": [{"delta": {"content": "hi"}}]}}
    check("stream 原样透传", asyncio.run(f.stream(ev)) == ev, "被改写")
    body = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a1"}]}
    check("outlet 返回同一 body", asyncio.run(f.outlet(body, {})) is body, "被改写")
    check("outlet 在无 assistant 消息时不报错",
          asyncio.run(f.outlet({"messages": []}, {})) == {"messages": []}, "报错")

    print("\n== T16 Filter 钩子签名与框架注入名匹配 ==")
    import inspect
    params = list(inspect.signature(f.stream).parameters)
    check("stream 只接受 event（filter.py:136 注入名）", params == ["event"], params)
    params = list(inspect.signature(f.inlet).parameters)
    check("inlet 接受 body/__user__", params == ["body", "__user__"], params)
    params = list(inspect.signature(pipe_mod.Pipe().pipe).parameters)
    check("pipe 接受 body/__user__/__event_emitter__",
          params == ["body", "__user__", "__event_emitter__"], params)

    print("\n== 汇总 ==")
    passed = sum(1 for _, ok in RESULTS if ok)
    failed = [n for n, ok in RESULTS if not ok]
    print("%d/%d passed" % (passed, len(RESULTS)))
    if failed:
        print("FAILED:", failed)
    return 0 if not failed else 1


def _raises(exc_type, func):
    try:
        func()
    except exc_type:
        return True
    except Exception as exc:  # 类型不符也算失败
        print("       (抛出的是 %s: %s)" % (type(exc).__name__, exc))
        return False
    return False


def _run_pipe(pipe_obj, body, user, emitter):
    """跑一次 pipe()，兼容返回字符串和返回异步生成器两种情况。"""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(pipe_obj.pipe(body, user, __event_emitter__=emitter))
        if isinstance(result, str):
            return result
        return loop.run_until_complete(collect(result))
    finally:
        loop.close()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        code = 2
    finally:
        server.shutdown()
    sys.exit(code)