# AI 测试 Agent：Web2 内部转账 MVP 设计

## 1. 项目背景

本项目用于构建一个可实际运行、可演示、可解释的 AI 测试平台作品集，同时体现测试开发能力和 Agent 工程能力。

当前项目包含 App、Web 用户端和后台三个平台。第一版只覆盖 Web 用户端的 Web2 内部转账流程，后续再扩展到链上转账、充值、后台和 App。

现有可复用资产：

- Open WebUI：本地模型和知识库的交互入口。
- Ollama：本地运行 `qwen3.5:9b`。
- Web2 PRD 及人工测试用例、评审稿。
- 禅道 Bug 结构化查询服务，当前已同步产品 9 的 Bug 数据。
- 独立测试环境和可修改数据的测试账号。

## 2. 项目目标

第一版需要完成一条真实闭环：

```text
Open WebUI 发起任务
→ 分析内部转账需求
→ 查询历史 Bug
→ 生成测试场景和测试 DSL
→ 人工审批
→ Playwright 执行
→ 采集网络请求
→ 校验 UI、接口和数据变化
→ 分析失败
→ 生成测试报告
```

项目需要证明：

1. 本地大模型能够参与需求理解、风险识别、测试设计和失败分析。
2. LangGraph 能够管理状态、条件分支、重试、暂停和恢复。
3. 确定性代码能够安全、可靠地执行测试和完成断言。
4. Agent 生成结果能够通过人工基准集进行量化评估。
5. 系统能够在不修改流程节点的情况下替换模型。

## 3. 非目标

第一版不实现：

- 链上转账、充值、红包、后台和 App 测试。
- 多 Agent 协作。
- 云端模型调用。
- LangSmith 等云端可观测平台。
- 自动提交禅道 Bug。
- 自动生成并执行任意 Python 或 Playwright 源代码。
- 多浏览器、移动端和大规模并发测试。
- 生产环境测试。

## 4. 总体架构

采用独立 Agent 服务，Open WebUI 仅作为交互和展示入口。

```text
┌──────────────────────────────┐
│ Open WebUI                   │
│ 发起任务、审批、查看结果       │
└──────────────┬───────────────┘
               │ Pipe/适配接口
┌──────────────▼───────────────┐
│ AI Test Agent                │
│ FastAPI + LangGraph          │
├──────────────────────────────┤
│ ModelProvider                │
│ - OllamaProvider             │
│ - FutureProvider             │
├──────────────────────────────┤
│ Tools                        │
│ - PRD Loader                 │
│ - Bug Query Client           │
│ - Playwright Runner          │
│ - Network Collector          │
│ - Report Generator           │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│ Local Persistence            │
│ SQLite checkpoints + artifacts│
└──────────────────────────────┘
```

### 4.1 组件边界

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| Open WebUI | 对话、审批、结果展示 | 核心流程编排和测试执行 |
| LangGraph | 节点、状态、分支、重试、暂停恢复 | 业务判断和页面操作细节 |
| 本地模型 | 需求分析、风险识别、测试设计、失败归因 | 环境保护和最终测试判定 |
| 确定性代码 | Schema 校验、Playwright执行、断言、清理、报告统计 | 开放式业务推理 |
| 测试人员 | 规则确认、方案审批、结果验收 | 重复执行机械步骤 |

## 5. 模型策略

### 5.1 第一版模型

- 模型运行方式：Ollama。
- 默认模型：`qwen3.5:9b`。
- 运行时禁止调用云端模型。
- Prompt、模型参数和输出 Schema 按节点独立配置。

### 5.2 可替换模型接口

所有模型节点只能依赖统一的 `ModelProvider` 接口，不允许直接调用 Ollama SDK。

概念接口：

```text
generate_structured(
    task_type,
    messages,
    output_schema,
    model_options
) -> ValidatedModelOutput
```

第一版实现 `OllamaProvider`。后续可以增加其他本地模型或云端 Provider，用相同输入、基准集和指标做效果对比。

### 5.3 模型约束

- 模型必须返回符合节点 Schema 的结构化结果。
- Schema 校验失败时，携带错误信息最多重试两次。
- 连续失败后流程暂停，不允许无限重试。
- 模型推断内容必须标注 `inferred=true` 和推断依据。
- PRD 未明确的信息不得表述为已确认需求。

## 6. LangGraph 流程

### 6.1 节点定义

| 节点 | 类型 | 输入 | 输出 |
| --- | --- | --- | --- |
| `initialize_task` | 确定性 | 用户指令、会话信息 | `task_id`、初始状态 |
| `environment_guard` | 确定性 | 环境配置 | 安全检查结果 |
| `load_sources` | 确定性 | PRD、人工用例、配置 | 规范化来源及版本信息 |
| `extract_requirements` | 模型 | 来源内容 | 结构化内部转账需求 |
| `validate_requirements` | 确定性 | 需求结构 | 校验结果和缺失项 |
| `analyze_risks` | 模型 | 需求、已有用例 | 歧义、风险和补充问题 |
| `retrieve_bugs` | 工具 | 关键词、条件 | 历史 Bug 列表 |
| `generate_test_plan` | 模型 | 需求、风险、Bug | 测试场景和 DSL |
| `validate_test_plan` | 确定性 | 测试 DSL | Schema、安全和覆盖校验 |
| `human_review` | 中断节点 | 测试方案 | 批准、驳回、补充或取消 |
| `prepare_execution` | 确定性 | 已批准方案、账号配置 | 可执行任务和测试数据快照 |
| `execute_tests` | 确定性 | 测试 DSL | UI 执行结果、截图、日志 |
| `collect_network` | 确定性 | 浏览器上下文 | 已观测接口清单 |
| `verify_results` | 确定性 | 执行前后数据、接口结果 | 断言和最终通过/失败结果 |
| `classify_failure` | 模型 | 失败证据、历史 Bug | 失败分类和分析说明 |
| `generate_report` | 确定性 | 完整任务状态 | JSON、Markdown、HTML 报告 |

### 6.2 条件分支

- 非测试环境：立即终止。
- 需求缺少关键业务规则：进入人工补充，随后返回需求分析。
- 模型结构校验失败：最多重试两次，之后暂停。
- 测试方案驳回：携带人工反馈返回 `generate_test_plan`。
- 测试方案取消：保存现有产物后结束。
- 环境或网络错误：重试一次。
- 产品功能失败：不自动重跑，直接保存证据并进入失败分析。
- 全部测试通过：直接生成报告。

## 7. Agent 状态

每个任务使用独立 `task_id`，主要状态字段如下：

```text
task_id
thread_id
status
source_versions
environment
requirements
requirement_gaps
risks
related_bugs
test_plan
approval
execution_snapshot
execution_results
network_inventory
failure_analysis
report_paths
node_history
created_at
updated_at
```

LangGraph checkpoint 保存流程恢复所需状态；体积较大的截图、HAR、日志和报告保存在任务产物目录，状态中只保存路径和摘要。

## 8. 测试 DSL

### 8.1 设计原则

- 模型生成结构化测试描述，不生成任意可执行代码。
- 只允许执行器注册过的 `action` 和 `assertion`。
- DSL 经过 Schema、环境和权限校验后才能执行。
- 每个场景必须记录来源：PRD、历史 Bug、人工基准或 Agent 推断。

### 8.2 示例

```json
{
  "case_id": "TC-OTI-002",
  "title": "内部转账成功",
  "priority": "P0",
  "source_refs": ["人工基准:TC-OTI-002"],
  "inferred": false,
  "preconditions": [
    "付款账号余额充足",
    "收款账号有效"
  ],
  "steps": [
    {"action": "open_internal_transfer"},
    {"action": "select_asset", "value": "USDT"},
    {"action": "fill_recipient", "source": "recipient_account"},
    {"action": "fill_amount", "value": "10"},
    {"action": "submit"},
    {"action": "complete_security_verification"}
  ],
  "assertions": [
    {"type": "payer_balance_decreased", "amount": "10"},
    {"type": "recipient_balance_increased", "amount": "10"},
    {"type": "transaction_record_created"}
  ]
}
```

### 8.3 初始动作集合

- `login`
- `open_internal_transfer`
- `select_asset`
- `fill_recipient`
- `fill_amount`
- `submit`
- `complete_security_verification`
- `refresh_transaction_history`

### 8.4 初始断言集合

- `page_loaded`
- `validation_message_equals`
- `request_not_sent`
- `transfer_request_succeeded`
- `payer_balance_decreased`
- `recipient_balance_increased`
- `transaction_record_created`
- `single_transaction_created`

## 9. Open WebUI 审批交互

测试方案生成后，LangGraph 在 `human_review` 节点暂停并保存状态。

支持的用户回复：

- `批准`：继续执行。
- `驳回：<原因>`：返回生成节点并携带反馈。
- `补充：<内容>`：更新任务上下文并重新分析。
- `取消`：终止并保留当前产物。

Open WebUI 会话 ID 映射到 LangGraph `thread_id`，保证同一对话可以恢复对应任务。

## 10. Web 与接口测试策略

### 10.1 UI 驱动接口发现

由于项目没有维护 Swagger，第一版通过 Playwright 执行 Web 流程并采集 XHR、fetch 和相关 HTTP 请求。

生成的已观测接口清单包含：

- 页面步骤和请求之间的关联。
- 请求方法、路径、参数和请求体结构。
- 响应状态码、响应结构和耗时。
- 认证方式摘要。
- 是否为写操作及潜在副作用。

该清单属于“已观测接口契约”，不能冒充开发确认的正式 API 规范。

### 10.2 联合断言

一次成功内部转账至少验证：

1. 页面提示成功。
2. 转账接口返回成功。
3. 付款方余额按规则减少。
4. 收款方余额按规则增加。
5. 付款方交易记录新增一条。
6. 重复提交不会生成多笔交易。

## 11. MVP 测试范围

### 11.1 人工基准场景

1. 内部转账页面正常打开。
2. 正常内部转账成功。
3. 收款人为空时禁止提交。
4. 金额为空、为 0 或非法时禁止提交。
5. 余额不足时禁止转账。
6. 重复点击只能产生一笔交易。

模型可以补充自转账、收款人不存在、金额精度和验证失败等场景，但必须标记为 Agent 推断。

### 11.2 数据和账号

- 使用独立测试环境。
- 至少准备付款账号和收款账号。
- 凭据通过本地忽略文件或环境变量提供，不写入代码、数据库日志和报告。
- 执行前记录余额和交易记录快照。
- 测试数据使用唯一 `task_id` 标识。

## 12. 安全设计

- 测试环境域名使用显式允许列表。
- 检测到生产域名或未知域名立即终止。
- 审批前禁止执行任何转账操作。
- DSL 不允许任意代码、Shell、SQL 或自定义 URL。
- 日志和报告脱敏 Cookie、Token、手机号、邮箱及账号凭据。
- 写操作必须携带当前任务 ID 和幂等保护信息（若业务接口支持）。
- 每个任务限制最大节点次数、模型重试次数和执行时长。

## 13. 产物与可观测性

任务产物目录：

```text
artifacts/<task_id>/
├── requirements.json
├── risks.json
├── related_bugs.json
├── test_plan.json
├── execution_results.json
├── network_inventory.json
├── screenshots/
├── traces/
├── report.md
└── report.html
```

每个节点记录：

- 开始和结束时间。
- 输入输出摘要。
- 模型名称及参数。
- Prompt 版本。
- Token 或上下文长度（接口可提供时）。
- 重试次数。
- 错误分类。
- 产物路径。

## 14. 错误处理

| 错误类型 | 处理方式 |
| --- | --- |
| 模型格式错误 | 携带 Schema 错误重试，最多两次 |
| 模型服务不可用 | 暂停任务并保留 checkpoint |
| 测试环境不可达 | 重试一次，之后标记环境阻塞 |
| 页面定位失败 | 保存截图和 DOM 摘要，标记自动化维护问题 |
| 接口超时或 5xx | 保存请求证据，进入失败分析 |
| 断言失败 | 不重跑，记录产品失败候选 |
| 凭据失效 | 暂停并提示更新本地配置 |
| 未知异常 | 保存完整节点轨迹并安全终止 |

## 15. 测试与评估

### 15.1 Agent 单元测试

- 每个模型节点使用固定响应或 Fake Provider 测试 Schema。
- 每个条件分支均有独立测试。
- Checkpoint 暂停和恢复测试。
- 模型重试上限测试。
- Provider 替换测试。

### 15.2 DSL 和执行器测试

- 非法动作和断言拒绝测试。
- 未审批执行拦截测试。
- 非测试域名拦截测试。
- 凭据脱敏测试。
- Playwright动作映射测试。
- 余额使用精确数值类型进行断言。

### 15.3 生成质量评估

人工用例作为 Golden Set（人工基准集）：

- 六条基准场景覆盖率。
- PRD 明确要求召回率。
- 无依据推断数量。
- 重复用例比例。
- Schema 首次通过率和重试后通过率。
- 历史 Bug 关联准确率。

## 16. 验收标准

- 能从 Open WebUI 发起任务并获得最终报告。
- 全流程只调用本地 `qwen3.5:9b`。
- 六条人工基准场景覆盖率为 100%。
- 最终结构化输出字段完整率为 100%。
- 审批前不会执行转账。
- 流程中断后可以从 SQLite checkpoint 恢复。
- 每条用例都有步骤、截图、接口记录、断言和结果。
- 成功转账能够验证双方余额及交易记录。
- 重复提交能够验证只生成一笔交易。
- 切换 ModelProvider 不需要修改 LangGraph节点代码。
- 测试域名保护、DSL 安全限制和敏感信息脱敏通过测试。

## 17. 实施边界

第一阶段只建立可运行的内部转账闭环。链上转账、后台、App、多 Agent、自动提 Bug 和模型横向评测均作为后续独立迭代，不进入本设计的实现范围。

