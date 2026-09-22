# open-webui 接管记录：部署加固 + 二次开发插件 + 集成自测

这是对一份 **open-webui 自托管部署**做接管（审计 → 修复 → 加固 → 实测）之后产出的全部成果与证据。
上游代码本身不在这里，本仓库放的是**我们对上游做的改动、二开插件、自测套件和运维脚本**，可以直接覆盖回一份 open-webui 检出使用（见「六、怎么用」）。

| 项目 | 值 |
| --- | --- |
| 上游检出 | `open-webui/open-webui`，浅克隆单提交 `0a7c15832fb30b1903753e83f81dc7d27e5b0944` |
| 运行镜像 | `ghcr.io/open-webui/open-webui:main`（6.51GB，build `8bd8b4fac5e059578ac0c74b3c18d11139f88b7d`） |
| 应用版本 | `0.11.4` |
| 容器内 Python | 3.11.16 / httpx 0.28.1 / pydantic 2.13.4 |
| 最终状态 | 容器 `Up (healthy)`，仅监听 `127.0.0.1:3000`，注册的第一个账号即管理员 |
| 插件自测 | **45/45 通过**（`selftest/run_tests.py`，在容器内跑） |

---

## 一、修掉的 4 个插件缺陷

二开插件（`custom_pipes/`）在被接管时**从来没跑起来过**，逐个定位到下面 4 个问题：

| # | 位置 | 症状 | 根因 | 修法 |
| --- | --- | --- | --- | --- |
| 1 | `custom_pipes/custom_model_pipe.py:109`（旧） | 整个文件 `SyntaxError: invalid syntax`，插件根本无法导入 | 残留了半截 `await __event_emitter__(` 与错位缩进 | 重写为完整的 `if __event_emitter__:` 状态事件块 |
| 2 | `custom_pipes/audit_filter.py:58` | 挂上 Filter 后流式请求直接 `TypeError: stream() missing 1 required positional argument` | 本版本 `utils/filter.py:136` 对 stream 类型注入的参数名是 **`event`**（`{'event': form_data}`），不是 `form_data` | 改成 `async def stream(self, event: dict) -> dict:` |
| 3 | `custom_pipes/custom_model_pipe.py:46` | 模型下拉框显示成 `Customdeepseek-chat` | `functions.py:110` 是 `f'{function_module.name}{sub_pipe_name}'` 裸拼接，没有分隔符 | `self.name` 由 `"Custom"` 改为 `"Custom: "` |
| 4 | `custom_pipes/ARCHITECTURE.md` | 文档行号漂移，照着读会找不到代码 | 上游重构后行号变了；且完全漏了第 4 种钩子 | 逐条实测更正：`utils/chat.py:218→151`、`utils/filter.py:192→190`、`middleware.py:2642→2639`；补上 `request()` 钩子（`middleware.py:3104/3408/5983/6203`）与「钩子名 = 方法名」「插件是实例化的」两条关键前提 |

## 二、部署侧 6 处优化（`deploy/docker-compose.yaml`）

| # | 改动 | 为什么 | 怎么验证的 |
| --- | --- | --- | --- |
| 1 | 端口由 `${OPEN_WEBUI_PORT-3000}:8080` 改为 `${OPEN_WEBUI_HOST-127.0.0.1}:${OPEN_WEBUI_PORT-3000}:8080` | 默认只绑本机。WebUI 在完成初始化前是可注册状态，直接 `0.0.0.0` 会把整个局域网暴露成一个可抢注的管理后台 | `docker inspect` → `ports={"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"3000"}]}` |
| 2 | `WEBUI_SECRET_KEY=` → `WEBUI_SECRET_KEY_FILE=/app/backend/data/.webui_secret_key` | 密钥落盘到数据卷：容器重建/升级后已登录用户的会话不再全部失效 | 容器重建两次后登录态与数据都保持 |
| 3 | 去掉 `open-webui` 对 `ollama` 的 `depends_on`，并把 ollama 的 `pull_policy: always` 改为 `missing` | 上游每次 `docker compose up` 都会尝试重拉约 1.5GB 的 ollama 镜像；且没有 ollama 时 WebUI 本身完全可用，不该被它拖住 | `docker compose up -d open-webui` 不再触发拉取，`UP_EXIT=0` |
| 4 | 补 `healthcheck.start_period: 900s`（沿用镜像自带的探针命令） | 首次冷启动要从 HuggingFace 下约 930MB 向量模型，实测耗时 **1209s**。没有 start_period 时这段时间容器一直显示 `unhealthy`，会误导人和运维脚本 | `docker inspect` → `StartPeriod: 900000000000` |
| 5 | 补 `logging` 轮转（`json-file`，`max-size=10m`，`max-file=3`） | 启动期日志量很大（下载进度 + 每个文件的 xet 日志），不限制会把宿主磁盘吃掉 | `docker inspect` → `log={"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}}` |
| 6 | 新增 `.env.example` | 把监听地址/端口/镜像标签/HF 相关开关集中说明，避免再去翻 compose | `docker compose config --quiet` → `CONFIG_EXIT=0` |

## 三、最花时间的一个坑：向量模型下载静默卡死

**现象**：容器启动卡在下载 `sentence-transformers/all-MiniLM-L6-v2`，缓存连续 10 分钟停在 980M 一动不动，日志也不报错。

**定位过程**（都在容器内实测，不是猜）：

- 最后一条 xet 日志：`Concurrency control for download: Decreased concurrency from 17 to 16; reason: transfer failed (success_ratio = 1.000, threshold = 0.500)` —— 它把并发从 4 一路加到 **18**，然后就没消息了。
- `/proc/net/tcp` 只剩 1 条 `ESTABLISHED` + 1 条 `CLOSE_WAIT`；进程 1 停在 `futex_wait_queue`，102 个线程全在睡。
- 同时用容器内 python 请求 `https://huggingface.co/api/models/...` → **200 / 1.2 秒**。

**结论**：不是网络不通，是 **hf_xet 的高并发多连接下载穿代理（本地中继 → mihomo）会被拖死，而且是静默挂起**——没有超时、没有异常。

**修复**：`HF_HUB_DISABLE_XET=1`（外加 `HF_HUB_DOWNLOAD_TIMEOUT=60`、`HF_HUB_ETAG_TIMEOUT=30` 兜底），退回单连接 HTTP。
**效果**：缓存 980M → 1.1G，约 **0.9MB/s**（比卡死的 xet 更快），**1209s 后容器 HEALTHY**。

顺带查清了浪费的根因：`backend/open_webui/retrieval/utils.py:1685 get_model_path()` 调 `snapshot_download(...)` 时**没有传 `ignore_patterns`**，于是把仓库 **30 个文件 / 931.7MB** 全下了一遍——同一个模型同时下了 `model.safetensors`、`pytorch_model.bin`、`tf_model.h5`、`rust_model.ot`、`onnx/*`、`openvino/*`。
省流办法（已写进 `.env.example`）：把 `RAG_EMBEDDING_MODEL` 指向一个**已存在的本地目录**，`get_model_path()` 里 `os.path.exists(model)` 为真会直接 return，完全跳过联网下载。

## 四、集成自测（`selftest/run_tests.py`）

这个自测不是静态检查：它把插件拷进**正在运行的容器**，用镜像自带的 httpx/pydantic 真实加载，并起一个本地模拟的 OpenAI 兼容 SSE 服务，逐条断言插件行为。

```bash
docker cp <open-webui检出>/custom_pipes open-webui:/tmp/selftest_pipes
docker cp selftest/run_tests.py open-webui:/tmp/run_tests.py
docker exec open-webui sh -c "rm -rf /tmp/selftest_pipes/__pycache__ && python /tmp/run_tests.py /tmp/selftest_pipes"
# -> 45/45 passed
```

覆盖范围（结果 45/45）：

- **编译**：两个插件的语法（这一步就是缺陷 #1 的回归防线）
- **Pipe**：未配置 Key 时只暴露 `error` 项；`model_ids` 的逗号/空格/空项解析；地址校验（拒绝非 http(s)、含账号密码、含查询参数，默认拒绝明文 http，开关放行）
- **Pipe 流式 happy path**：内容拼接、`status(开始)`/`status(完成)` 两个事件、请求路径 `/chat/completions`、`model` 前缀剥离、强制 `stream=True`、UserValves 生效、白名单字段透传 + 非白名单字段丢弃、`Authorization` 与 `Accept` 头
- **Pipe 异常分支**：HTTP 500、端口不通、缺 Key、空 messages、空 model、脏 SSE（坏 JSON / 空 choices / 无 content）全部容错且都收尾
- **Filter**：`inlet` 原地注入系统提示 / 关键词拦截（大小写归一）/ 长度上限拦截、`stream` 原样透传、`outlet` 返回同一 body
- **钩子签名**：直接断言 `stream` 只接受 `event`、`inlet` 接受 `body/__user__`、`pipe` 接受 `body/__user__/__event_emitter__`——把缺陷 #2 那类「注入名写错」永久钉死在测试里

自测过程中还发现 3 处**测试脚本自身**的错（模拟服务按 URL 路径分流，而插件永远 POST 到 `/chat/completions`；断言里协程被提前求值；测试数据里 `BadWord` 含被拦词 `bad`），已全部修正——插件代码本身没挂。

## 五、文件清单

```
custom_pipes/            二开插件（manifold Pipe + Filter）
  ARCHITECTURE.md        源码导读：钩子模型、注入参数、行号索引、接管记录、网络问题记录
  README.md              导入步骤与注意事项
  custom_model_pipe.py   任意 OpenAI 兼容 API 的流式 Pipe（DeepSeek/Kimi/vLLM/Ollama…）
  audit_filter.py        inlet/stream/outlet 三钩子示例：改写、监控、审计
deploy/
  docker-compose.yaml    加固后的部署文件（相对上游的改动都在文件头注释里）
  .env.example           监听地址/端口/镜像标签/HF 开关说明
selftest/
  run_tests.py           容器内集成自测（45 项断言，自带模拟 SSE 服务）
scripts/
  relay_7897.py          把只监听 127.0.0.1 的本机代理转发给 WSL 里的 Docker 引擎
  monitor_openwebui.py   启动期监控：容器状态 + 模型缓存体积，带截止时间
  poll_health.py         快速探活（/health + /api/config）
  verify_login.py        只读登录面探测（不建账号）
```

## 六、怎么用

把本仓库内容覆盖回一份 open-webui 检出即可：

```bash
git clone https://github.com/open-webui/open-webui.git && cd open-webui
cp -r <本仓库>/custom_pipes .            # 二开插件
cp <本仓库>/deploy/docker-compose.yaml . # 覆盖部署文件（也可只摘你需要的那几项）
cp <本仓库>/deploy/.env.example . && cp .env.example .env
docker compose up -d
```

插件导入方式（Admin Panel → Functions → 新建 → 粘贴代码，或走 API）见 `custom_pipes/README.md`。
注意 `custom_pipes/` 是我们自己的代码，`docker-compose.yaml` 派生自上游（MIT），保留上游署名。

## 七、已知限制 / 还没做的

1. **代理中继是会话级的**：本机代理只监听 `127.0.0.1:7897`，Docker 引擎跑在 WSL 里够不到宿主 loopback，靠 `scripts/relay_7897.py` 转发（会话结束就没了）。长期方案是让 Clash 开「允许局域网连接」并把 Docker 代理指向宿主局域网 IP，或做成计划任务/服务。
2. **向量模型仍然是联网下载的**：缓存已经下完（1.1G），但每次启动仍会做 ETag 检查；彻底离线需要按第三节的办法把模型固定成本地目录。
3. **还没有真正跑通一次模型对话**：容器、鉴权、插件逻辑都验证过了，但真实模型调用需要填入你自己的 API Key（Admin Panel → Functions → 本插件 → Valves），填完即可用。
4. 注册的第一个账号即管理员；注册成功后框架会把 `ui.enable_signup` 自动落库为 `false`（`main.py:374`），此后新用户需要管理员在后台创建。