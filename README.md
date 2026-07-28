# 项目知识库：结构化 Bug 查询

这个服务把禅道 Bug 同步到本地 SQLite，并通过只读 OpenAPI 给 Open WebUI 提供精确查询能力。PRD 等文档继续使用知识库检索；按 Bug 编号或字段查询时使用本工具。

## 1. 安装

```bash
cd /Users/maoyijiu/Documents/tg-work/knowledge_base
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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
