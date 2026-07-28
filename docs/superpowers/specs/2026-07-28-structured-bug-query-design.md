# 结构化 Bug 查询服务设计

## 背景

Open WebUI 的知识库检索依赖向量相似度，适合查询 PRD 等非结构化文档，但不适合按 Bug 编号和固定字段做精确查询。Bug 1227 已存在于 CSV 中，却无法被稳定召回，因此需要把 Bug 数据接入结构化查询工具。

## 目标

- 保持现有 BI 导出脚本不变。
- 从禅道产品 9 独立同步完整 Bug 字段到 SQLite。
- 提供只读 HTTP API 和 OpenAPI 描述，供 Open WebUI 作为工具调用。
- 支持按 Bug 编号精确查询，以及按关键词、状态、严重程度、模块和解决方案筛选。
- 手动执行同步和启动，不设置开机自动启动。

## 数据字段

| 中文字段 | 数据库/API 字段 | 禅道来源 |
| --- | --- | --- |
| Bug编号 | `bug_id` | `id` |
| 产品 | `product` | `productName`，列表缺失时使用同步参数 |
| 模块 | `module` | `moduleTitle`；模块 ID 为 0 时写入“未设置” |
| 标题 | `title` | `title` |
| 严重程度 | `severity` | `severity` |
| 优先级 | `priority` | `pri` |
| 状态 | `status` | `status` |
| 类型 | `bug_type` | `type` |
| 重现步骤 | `reproduction_steps` | `steps`，HTML 转为可读文本 |
| 创建人 | `created_by` | `openedBy` |
| 负责人 | `assigned_to` | `assignedTo` |
| 创建时间 | `created_at` | `openedDate` |
| 解决人 | `resolved_by` | `resolvedBy` |
| 解决方案 | `resolution` | `resolution` |
| 解决时间 | `resolved_at` | `resolvedDate` |
| 关闭时间 | `closed_at` | `closedDate` |
| 是否重新激活 | `is_reopened` | `activatedCount` 等字段 |

API 同时返回状态、类型和解决方案的中文标签，但保留原始编码，避免翻译导致筛选失真。

## 组件

1. `ZenTaoClient`：登录、重试、分页获取产品 Bug，按需补充模块名称。
2. `BugRepository`：初始化 SQLite、原子替换全部数据、精确查询和组合筛选。
3. `FastAPI`：提供 `/health`、`/bugs/{bug_id}`、`/bugs` 和 `/openapi.json`。
4. 同步命令：显式读取指定 `.env`，不复制或输出账号密码。

## 安全边界

- Open WebUI 只能调用只读查询接口，不能触发禅道同步。
- 凭据仅在手动同步进程中读取，不写入数据库或 API。
- 服务默认仅供本机使用；为了让 Docker 中的 Open WebUI 访问，启动时监听 `0.0.0.0`。

## 验收标准

- 单元测试覆盖字段转换、原子替换、精确查询、筛选和 API 404。
- 使用真实禅道数据完成一次同步。
- `/bugs/1227` 返回标题、严重程度、状态、解决方案、创建时间和关闭时间。
- Open WebUI 可通过 `http://host.docker.internal:8765/openapi.json` 添加工具。
