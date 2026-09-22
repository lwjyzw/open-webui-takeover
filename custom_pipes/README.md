# Custom Pipes — 二开实验场

存放自定义 Pipe / Filter / Tool 脚本。这些文件**不会被主程序自动加载**，
需要通过管理界面导入（这是 Open WebUI 插件的设计：插件存数据库、运行时热加载）。

## 导入步骤

1. 启动服务：`cd backend && pip install -e . && open-webui serve`（需 Python 3.11），
   或者直接 `docker compose up -d`（推荐，见仓库根目录 docker-compose.yaml）。
2. 注册管理员账号（首次打开 http://127.0.0.1:3000 时创建的第一个账号即为 admin（本机 compose 已把端口绑定到 127.0.0.1:3000，详见 ARCHITECTURE.md 第八节））。
3. 右上角头像 → Admin Panel → Functions → 「+」新建 Function。
4. 把 `custom_model_pipe.py` 的全部内容粘贴进编辑器，保存。
5. 回到 Functions 列表，打开该插件的开关（Toggle）。
6. 点击插件右侧齿轮配置 Valves：填入 `api_base` / `api_key` / `model_ids`。
7. 回到首页，模型下拉框会出现 `Custom: deepseek-chat` 等模型项（前缀由 `self.name` 决定，框架只做字符串拼接，见 ARCHITECTURE.md 七.4），发消息测试流式输出。

## 常用 API 端点参考（均为 OpenAI 兼容）

| 服务商 | api_base | 模型示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | moonshot-v1-8k |
| 阿里通义（兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus |
| 本地 Ollama | `http://host.docker.internal:11434/v1`（Docker 内） | llama3.1 |
| vLLM 自部署 | `http://<host>:8000/v1` | 按部署名 |

## 调试

- Pipe 内异常会显示在后端日志：`docker compose logs -f` 或启动终端输出。
- 修改代码后在编辑器重新保存即可热加载，无需重启。
- `__event_emitter__` 可发状态提示 / 插入消息 / 替换内容，事件类型见
  `backend/open_webui/utils/middleware.py` 中 `get_event_emitter` 相关代码。

## 本目录约定

- `custom_model_pipe.py` — Pipe（自定义模型接入，manifold 模式）
- 后续可加：`*_filter.py`（输入/输出钩子）、`*_tool.py`（模型可调用的函数）
