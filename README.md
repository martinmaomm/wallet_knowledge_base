# 项目知识库：结构化 Bug 查询

这个服务把禅道 Bug 同步到本地 SQLite，并通过只读 OpenAPI 给 Open WebUI 提供精确查询能力。PRD 等文档继续使用知识库检索；按 Bug 编号或字段查询时使用本工具。

## 1. 安装

```bash
cd /Users/maoyijiu/Documents/tg-work/knowledge_base
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 2. 同步禅道 Bug

复用现有 BI 项目的 `.env`，只读取凭据，不修改原项目：

```bash
.venv/bin/python -m bug_service.sync \
  --env-file /Users/maoyijiu/Documents/tg-work/BI/.env \
  --product-id 9 \
  --product-name 内部钱包
```

同步会在一个 SQLite 事务内替换全部数据。成功前，查询服务仍读取上一版完整数据。

## 3. 启动查询服务

```bash
.venv/bin/uvicorn bug_service.api:app --host 0.0.0.0 --port 8765
```

常用地址：

- 健康检查：`http://localhost:8765/health`
- Bug 1227：`http://localhost:8765/bugs/1227`
- API 文档：`http://localhost:8765/docs`
- OpenAPI：`http://localhost:8765/openapi.json`

## 4. 接入 Open WebUI

在 Open WebUI v0.10.2 中打开：

`用户菜单 → 管理员面板 → 设置 → 扩展功能 → External Tool Servers`

点击添加按钮，选择 `OpenAPI`，认证方式选择“无”，URL 填写：

```text
http://host.docker.internal:8765/openapi.json
```

把工具命名为“禅道 Bug 精确查询”，先验证连接，再依次保存连接和管理员设置。回到对话后，选择 `qwen3.5:9b`，从“扩展功能 → 工具”中启用它。可以用下面的问题验证：

```text
查询 Bug 1227，返回 Bug 标题、严重程度、状态、解决方案、创建时间和关闭时间。必须调用禅道 Bug 精确查询工具。
```

## 5. 查询示例

```bash
curl http://localhost:8765/bugs/1227
curl 'http://localhost:8765/bugs?status=closed&severity=2&limit=10'
curl 'http://localhost:8765/bugs?keyword=提现&limit=20'
```

## 6. 测试

```bash
.venv/bin/python -m pytest -q
```

## 7. AI 测试 Agent 本地配置

AI 测试 Agent 使用项目根目录下的 `.env` 读取本地模型、测试环境和运行数据配置。该文件已加入 `.gitignore`，禁止提交测试账号、交易密码、浏览器认证状态或其他凭据。

| 参数 | 说明 |
|---|---|
| `OLLAMA_BASE_URL` | Ollama 本地模型服务地址，默认使用 `http://127.0.0.1:11434`。本机采用 IPv4 环回地址，避免 `localhost` 在不同环境解析到 IPv4 或 IPv6 所造成的连接差异；仍允许显式配置 `localhost` 或 `::1`。 |
| `OLLAMA_MODEL` | 本地推理模型名称，默认使用 `qwen3.5:9b`。 |
| `BUG_SERVICE_URL` | 当前项目的结构化 Bug 查询服务地址。 |
| `TEST_BASE_URL` | 钱包 Web2 测试环境地址，必须由使用者填写。 |
| `ALLOWED_TEST_ORIGINS` | 允许自动化访问的测试环境来源列表，多个来源使用英文逗号分隔。 |
| `AGENT_DB_PATH` | Agent 本地 SQLite 状态文件路径。 |
| `ARTIFACTS_DIR` | 截图、网络记录和测试报告等运行产物目录。 |
| `PLAYWRIGHT_STORAGE_STATE` | Playwright 浏览器认证状态文件路径。 |
| `AGENT_SOURCE_PATHS` | PRD、评审记录和测试用例等本地资料路径，多个路径使用英文逗号分隔。 |
| `TEST_PAYER_ACCOUNT` | 内部转账付款测试账号，仅保存在本机 `.env`。 |
| `TEST_RECIPIENT_ACCOUNT` | 内部转账收款测试账号，仅保存在本机 `.env`。 |
| `TEST_TRANSACTION_PASSWORD` | 测试环境交易密码，仅保存在本机 `.env`。 |
| `AGENT_API_TOKEN` | Agent API 使用的本地 Bearer Token。生成后只填写到项目根目录中已忽略的 `.env`，禁止提交。 |

可使用下面的命令生成 Token，然后仅填写到本机 `.env`。不要把真实
Token 写入 README、Pipe 源码或其他受版本控制的文件：

```bash
openssl rand -hex 32
```

启动 Agent API：

```bash
./scripts/run_agent.sh
```

`run_agent.sh` 使用 Uvicorn 的默认**单 worker**模式。当前同一
`thread_id` 的并发互斥锁属于进程内状态，因此不要增加 `--workers`
或启动多个 Agent API 实例；多进程并发控制需要后续单独设计。

### Playwright 证据安全

原生 Playwright trace 会记录 `fill` 参数、DOM 快照、网络请求和响应资源，
其中可能包含测试账号、交易密码、Cookie 或 Token。Runner 因此只把原生
trace 作为同目录内的短暂中间文件，并在返回结果前执行以下处理：

- 截图使用 Playwright `mask` 遮挡固定账号/密码输入框，以及页面上重复显示
  的收款账号、付款账号和交易密码文本。
- 最终 trace 仅保留经过递归脱敏且 `callId` 成对的动作与调用栈元数据。
- DOM 快照、screencast、原生 network trace 和 `resources/*` 全部删除；
  接口诊断改用受大小、深度和条目数限制的脱敏 `NetworkInventory`。
- 净化 ZIP 会逐 entry 扫描账号、密码及其 URL/JSON 编码变体；任何残留
  都会删除 trace，并把执行结果标记为 `EvidenceCaptureError`。
- artifact 目录权限固定为 `0700`，文件固定为 `0600`；随机临时文件使用
  排他创建和 `O_NOFOLLOW`（平台支持时），取消执行也会等待清理完成。

这是有意的安全取舍：净化后的 trace 不具备原生 trace 的完整页面回放和
响应资源查看能力。本地测试 Agent 优先保证秘密不进入长期产物。路径检查、
随机文件名和原子替换显著缩小了符号链接竞态窗口，但无法完全防御拥有同一
系统用户权限、可同时修改 artifact 目录的恶意进程；该场景需要操作系统级
用户隔离或独立沙箱。

## 8. 安装 Open WebUI 测试 Agent Pipe

当前 Open WebUI 运行在 Docker Desktop 容器中，而 Agent API 运行在
macOS 宿主机。把 `openwebui_tools/ai_test_agent.py` 导入 Open WebUI
的 Pipe Function 后，保留默认地址：

```text
http://host.docker.internal:8770
```

`host.docker.internal` 是 Docker Desktop 容器访问 macOS 宿主机的固定
gateway。只有在 Open WebUI 和 Agent API 都直接运行于宿主机时，才改用
`http://localhost:8770` 或 `http://127.0.0.1:8770`。Pipe 会拒绝其他
主机、端口、userinfo、路径、查询参数和重定向地址。

在 Open WebUI 容器环境中必须启用 Valve 加密：

- `ENABLE_VALVE_ENCRYPTION=true`
- `WEBUI_SECRET_KEY` 必须设置为独立生成、长期稳定且不会随容器重建变化的密钥。

`WEBUI_SECRET_KEY` 应通过 Docker Secret、受保护的部署环境变量或其他
本机秘密管理方式注入，不能提交到本仓库。更换或丢失该密钥可能导致已保存
的 Valve 凭据无法解密。

最后，在 Open WebUI 的 Pipe 配置界面把与项目 `.env` 中相同的
`AGENT_API_TOKEN` 填入 `AGENT_API_TOKEN` Valve。不要把真实 Token
写进 Pipe 文件；Valve 的持久化安全依赖上述 Open WebUI 加密配置。
