# Open WebUI 源码导读 — 消息主流程与插件挂载点

> 基于 2026-09 浅克隆版本。所有行号均可直接点击跳转。
> 建议按本文顺序读，一天可以通读主链路。

## 一、一条消息的完整生命周期

```
用户发送消息 (前端 Svelte)
  → POST /api/chat/completions          backend/open_webui/main.py (挂载路由)
  → generate_chat_completion            backend/open_webui/utils/chat.py:151
      ├─ 模型不存在？→ 尝试 task/直接补全
      ├─ model.pipe 存在？→ generate_function_chat_completion (functions.py:153)  ← 你的 Pipe
      ├─ owned_by == ollama？→ /ollama/api/chat
      └─ 其他 → generate_openai_chat_completion
  → process_chat_payload                backend/open_webui/utils/middleware.py:2365   【请求预处理】
  → 调用上游模型 API（ollama/openai routers）
  → process_chat_response               backend/open_webui/utils/middleware.py:6387   【响应后处理】
      ├─ streaming_chat_response_handler   middleware.py:4217（SSE 逐块解析、工具调用循环、citation）
      └─ non_streaming_chat_response_handler middleware.py:4033
  → outlet_filter_handler               middleware.py:3872
  → background_tasks_handler            middleware.py:3654（标题生成、tags、自动记忆等）
```

## 二、process_chat_payload 的处理顺序（middleware.py:2371 有官方注释）

源码注释原文：`Pipeline Inlet -> Filter Inlet -> Chat Memory -> Chat Web Search
-> Chat Image Generation -> Chat Code Interpreter -> Chat Tools Function Calling -> Chat Files`

即：你的 Filter 的 `inlet()` 在**所有**知识库检索、联网搜索、图片生成、工具调用**之前**执行——
这是做输入审计、prompt 改写的最早挂载点。

## 三、插件挂载点速查（全部走签名反射，写哪个参数就注入哪个）

| 插件类型 | 方法 | 调用位置 | 典型用途 |
|---|---|---|---|
| Pipe | `pipes()` + `pipe()` | functions.py:71 注册（manifold 判定在 87）、functions.py:310 执行 | 接入任意自定义模型 |
| Filter | `inlet()` | middleware.py:2639 | 输入审计/改写/拦截 |
| Filter | `request()` | middleware.py:3104 / 3408 / 5983 / 6203 | 请求体发给上游前最后改写 |
| Filter | `outlet()` | middleware.py:3872 outlet_filter_handler（调用点 3984） | 响应统计/后处理 |
| Filter | `stream()` | middleware.py:4842 / 6337 调用 | 逐块改写 SSE 事件（参数名固定为 `event`） |
| Tool | 工具函数 | middleware.py:1280 chat_completion_tools_handler | 让模型调用你的函数 |
| Action | `action()` | 聊天消息下方按钮 | 一键总结/翻译等按钮 |

钩子名 = 方法名：框架用 `getattr(plugin, filter_type)` 取方法，所以 Filter 类里写
`inlet` / `request` / `outlet` / `stream` 四种方法名就对应四种挂载点。
插件是被**实例化**的（`backend/open_webui/utils/plugin.py:298-304` 返回 `module.Filter()`），
所以 `self.file_handler = True` 这类实例属性是生效的。

反射实现：`backend/open_webui/utils/filter.py:190`（`inspect.signature(handler)`），
注入字典的构造在 filter.py:136——**stream 用 `event`，其余三种都用 `body`**；
Pipe 版在 `backend/open_webui/functions.py:202`。可注入参数包括
`__user__`、`__event_emitter__`、`__event_call__`、`__request__`、`__metadata__`、`__task__`、`__files__` 等
（完整清单见 functions.py:268-283）。

## 四、值得精读的文件（按优先级）

1. `backend/open_webui/functions.py` — 500 行不到，插件系统全部核心，读完即可写出全部六类插件
2. `backend/open_webui/utils/middleware.py` — 6400+ 行的大文件，**只读关键函数**：
   - `process_chat_payload` (2365) 预处理流水线
   - `chat_completion_tools_handler` (1280) 工具调用循环（agent 核心逻辑）
   - `streaming_chat_response_handler` (4217) SSE 解析与事件发射
3. `backend/open_webui/utils/plugin.py` — 插件如何从数据库字符串变成可 import 的模块
   （`load_function_module_by_id`, 259 行起；含 frontmatter 解析和依赖自动安装）
4. `backend/open_webui/models/` — SQLAlchemy 模型层，二开加新表从这里抄模式
5. `backend/open_webui/routers/` — 每个 REST 端点一个文件，API 风格参考

## 五、前端速览

- `src/lib/components/admin/Functions/FunctionEditor.svelte` — 在线插件编辑器（CodeMirror）
- `src/lib/apis/functions/` — Functions 的前端 API 封装
- 路由为 SvelteKit 文件式路由：`src/routes/`

## 六、实验场

`custom_pipes/` 目录（不在主程序加载路径内，仅作粘贴源）：

- `custom_model_pipe.py` — Pipe 示例：manifold 多模型、Valves/UserValves、流式、event emitter
- `audit_filter.py` — Filter 示例：inlet 拦截 + 系统提示注入、outlet 审计日志
- 导入方式见同目录 README.md

## 七、接管记录（2026-09-22 起）

接管时仓库实际状态：浅克隆（depth=1）单提交 `0a7c158`（2026-09-04），
本地改动仅 2 处 —— `docker-compose.yaml`（绑定 127.0.0.1、改用 `WEBUI_SECRET_KEY_FILE`）+ 未跟踪的 `custom_pipes/`。
**从未实际运行过**：无 `.env`、无 `backend/.venv`、无 `node_modules`。

已修缺陷（均已通过 `ast.parse` 校验）：

| 缺陷 | 位置 | 说明 |
|---|---|---|
| 语法错误 | `custom_model_pipe.py` 旧 109-112 行 | `for key in (...)` 循环体后残留半截 `await __event_emitter__(` 与错位缩进，整文件 `SyntaxError: invalid syntax`（line 110），根本无法粘贴进编辑器。已重写为正常的 `if __event_emitter__:` 块 |
| 钩子参数名错误 | `audit_filter.py` 的 `stream()` | 本版本对 stream 钩子注入的参数名**是 `event`**（`utils/filter.py:136`：`{'event': form_data} if filter_type == 'stream' else {'body': form_data}`）。原签名 `stream(self, form_data)` 在流式过滤启用时会 `TypeError: stream() missing 1 required positional argument`。已改为 `async def stream(self, event: dict)` |
| 文档行号漂移 | `ARCHITECTURE.md` | `chat.py:218` → 实际 **151**；`filter.py:192` → 实际 **190**；inlet 调用点 `middleware.py:2642` → 实际 **2639**；并补上此前完全没提的 `request()` 钩子（3104/3408/5983/6203） |
| 名称前缀拼接 | `custom_model_pipe.py` 的 `self.name` | 前缀是裸字符串拼接（`functions.py:110` `f'{function_module.name}{sub_pipe_name}'`），写 `"Custom"` 会显示成 `Customdeepseek-chat`。已改为 `"Custom: "` |

运行环境缺口（接管时未解决）：本机 Docker 引擎未启动（Docker Desktop 已装未运行，`docker ps` 连不上 `npipe:////./pipe/dockerDesktopLinuxEngine`）；
本机 Python 只有 3.13 / 3.14，而项目要求 `>= 3.11, < 3.13.0a1`（`pyproject.toml:131`）。
要跑起来需先启动 Docker Desktop（`docker compose up -d`），或装 Python 3.12 走本地模式。

## 八、启动实录与网络修复（2026-09-22 实测）

> 本节涉及本机环境的部分（安装路径、内网 IP）已用 `<...>` 占位符脱敏，数值本身与诊断结论无关。

**结论：容器已跑起来**，`open-webui` 监听 `127.0.0.1:3000`（镜像 `ghcr.io/open-webui/open-webui:main`，6.51GB）。

### 8.1 镜像拉取卡死（已解决）

首次 `docker compose pull open-webui` 永远停在 19 行 `Pulling fs layer 0B`，无任何字节。
判定卡死的**决定性证据**：Docker Desktop 的 WSL 虚拟磁盘
（`<DockerDesktop 安装目录>\wsl\disk\docker_data.vhdx`）45 秒增长 **0.0MB**
（注意 `%LOCALAPPDATA%\Docker` 下没有 vhdx，别找错位置）。

根因是 daemon 自己报出来的：

```
Error response from daemon: failed to resolve reference "docker.io/library/hello-world:latest":
... dialing registry-1.docker.io:443 container via direct connection because
Docker Desktop has no HTTPS proxy ... connectex: A connection attempt failed ...
```

直连探测对比（用 Python urllib；**不要用 `curl.exe` 判断网络** —— 本机 curl 的 schannel 已损坏，
报 `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`，所有请求返回 000 假阴性）：

| 目标 | 经 `127.0.0.1:7897` | 直连 |
|---|---|---|
| ghcr.io/v2/ | 401 | 401（时好时坏） |
| pkg-containers.githubusercontent.com | 400 | 400 |
| registry-1.docker.io/v2/ | 401 | **timed out** |
| auth.docker.io/token | 200 | **timed out** |

### 8.2 为什么不能把 Docker 直接指向 7897

代理是 `verge-mihomo.exe`（Clash Verge 的 mihomo 内核），**只监听 127.0.0.1:7897**；
而 daemon 跑在 WSL 虚拟机里（VM IP `<VM_IP>`，网关 `<WSL_网关IP>`），从 VM 打不到 host 的 loopback
（实测 `wget http://<WSL_网关IP>:7897/` → download timed out）。

解决：在 host 上跑一个 TCP 中继，把 7897 暴露到 VM 可达的网卡 ——
`scripts/relay_7897.py`（监听 `0.0.0.0:17897`，转发到 `127.0.0.1:7897`）。
验证：VM 内 `wget http://<WSL_网关IP>:17897/` 拿到 mihomo 的 `HTTP/1.1 400 Bad Request`；
经中继访问 ghcr.io / registry-1.docker.io / auth.docker.io 全部 401/200 正常。

### 8.3 Docker Desktop 代理配置（键名取自二进制）

`com.docker.backend.exe` 的 Go struct tag 给出准确键名，写入 `%APPDATA%\Docker\settings-store.json`：

```json
"proxyHttpMode": "manual",
"overrideProxyHTTP": "http://<WSL_网关IP>:17897",
"overrideProxyHTTPS": "http://<WSL_网关IP>:17897",
"overrideProxyExclude": "localhost,127.0.0.1"
```

（另有 `overrideProxyTCP` / `overrideProxyPAC`。）改完必须 `docker desktop stop` → 改文件 →
`docker desktop start`；运行中改会被覆盖。启动后 `docker info` 会显示
`HTTP Proxy: http.docker.internal:3128`（Docker Desktop 内置转发层，上游才是我们配的地址）。
**canary 验证**：`docker pull hello-world` 从 dial timeout 变为成功。

### 8.4 注意事项

- 中继进程是本会话的后台作业，**会话结束即消失**；长期使用建议在 Clash 里开「允许局域网」后直接填
  host 局域网 IP，或把中继注册成计划任务。
- WSL 网关 IP（用 `<WSL_网关IP>` 代指）若变化，需同步修改 `settings-store.json`。
- 回退：改之前先备份 `settings-store.json`。
- 本机 shell 是 **Windows PowerShell 5.1**（无 `UTF8NoBOM`）；写 JSON 用
  `[System.IO.File]::WriteAllText($p, $json, (New-Object System.Text.UTF8Encoding($false)))`。
- 容器内出网同样走通了（启动时从 HuggingFace 下载 `sentence-transformers/all-MiniLM-L6-v2` 成功）。

### 8.5 启动方式

```powershell
cd <open-webui 检出目录>
docker compose up -d --no-deps open-webui   # 先跳过 ollama（另一个 ~1.5GB 镜像）
```

注意 compose 里 ollama 服务带 `pull_policy: always`，不带 `--no-deps` 的 `docker compose up -d`
每次都会重新拉取它。

## 8.6 embedding 模型下载卡死（hf_xet）与最终修复

**现象**：容器 `running` 但 `unhealthy`，`/health` 始终连不上（TCP 能连、应用未监听）；
日志停在 `INFO: Waiting for application startup.`；HF 缓存先涨到 980M，然后 **10 分钟零增长**。

**定位**：
- 容器出网正常（容器内 `httpx.get('https://huggingface.co/api/models/...')` → 200 / 1.2 秒）。
- 容器内只剩 **1 条 ESTABLISHED** 连接，进程 1 停在 `futex_wait_queue`（102 个线程全睡）。
- xet 日志 `.../cache/embedding/models/xet/logs/xet_*.log` 显示它开了 **16~18 条并发连接**，
  最后一条活动停在 `12:01:08`，之后彻底静默，并伴随
  `Concurrency control for download: Decreased concurrency from 17 to 16; reason: transfer failed`。
  结论：**hf_xet 的高并发多连接下载被代理拖死**（经中继穿 mihomo，连接一多就烂），
  而 hf_hub 表现为「静默挂起」而不是报错。

**修复**（`docker-compose.yaml`，已备份为 `docker-compose.yaml.bak`）：

```yaml
- 'HF_HUB_DISABLE_XET=1'        # 关键：退回普通 HTTPS 下载，不再开十几条并发连接
- 'HF_HUB_DOWNLOAD_TIMEOUT=60'  # 卡住就报错重试，而不是永久挂起
- 'HF_HUB_ETAG_TIMEOUT=30'
```

`docker compose up -d --no-deps open-webui` 重建后：缓存 980M→1.0G→1.1G（约 0.9MB/s，**比 xet 还快**），
1209 秒后应用启动完成、转为 healthy。

**为什么一共要下 932MB**：`backend/open_webui/retrieval/utils.py:1685 get_model_path()` 调用
`snapshot_download(repo_id=..., cache_dir=..., local_files_only=...)` 时**没有 ignore_patterns**，
于是把 `sentence-transformers/all-MiniLM-L6-v2` 的**全部 30 个文件、931.7MB**
（tf/flax/rust/onnx×7/openvino 等所有框架格式）全部拉下来，而实际只需要 ~90MB 权重 + tokenizer。
缓存在 named volume 里，只下这一次。

> 以后若要省掉这 900MB：把 `RAG_EMBEDDING_MODEL` 指向一个**已存在的本地目录**即可 ——
> `get_model_path()` 在 `os.path.exists(model)` 时直接返回该路径，完全跳过 snapshot_download。

### 验证结果（2026-09-22 实测）

| 检查 | 结果 |
|---|---|
| `docker ps` | `open-webui` Up (healthy)，`127.0.0.1:3000->8080/tcp` |
| `GET /health` | 200 |
| `GET /api/config` | 200，`version=0.11.4`，`onboarding=true`，`enable_signup=true`，`auth=true` |
| `GET /` | 200，11318 字节，含 `<title>` |
| `GET /api/v1/auths/` `/api/v1/models` `/api/v1/users/` | 401 `{"detail":"Not authenticated"}` |
| `POST /api/v1/auths/signin`（错误口令） | 400 `{"detail":"The email or password provided is incorrect..."}` |

即：**服务已可用，浏览器打开 http://127.0.0.1:3000 注册的第一个账号即为管理员**（`onboarding: true`）。
