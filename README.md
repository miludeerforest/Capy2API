# Capy2API

将 **Capy 网页端的广告支持模型**转换为有限的 OpenAI 兼容 API，可接入 New API、Pi 及其他兼容客户端。

本项目使用用户自己的 Clerk 网页会话，调用 Capy 的 `/app` 接口；不使用公开的 `/api/v1` 推理路由，也不自动回退到付费模型。

> **实验性项目。** 已验证文本对话、Pi 工具调用闭环和实时事件接入，但不等同于原生 OpenAI API。模型可用性、网页内部协议及账号资格可能变化。目录中的零价格和测试时点账单未变化，**不能保证永久免费或没有云机器费用**。

## 功能概览

- 提供 `GET /v1/models` 和 `POST /v1/chat/completions`。
- 每次推理重新检查组织目录，只允许通过广告标记、可用状态、路由和零价格校验的模型；空名单和未知模型拒绝服务。
- 通过 Clerk cookie 换发短期 JWT；支持导入会话并持久化。
- 优先使用网页实时 WebSocket 事件，连接失败或断开时降级 REST 轮询。
- 支持基于文本协议的 OpenAI function tools：模型作决策，**客户端执行工具**。
- 在上游允许时增量转发最终文本；工具调用始终完整校验后发送。
- 响应完成后异步清理本次线程，提供阶段耗时、每步 usage 和清理状态查询。

## 工作原理

```text
Pi / OpenAI-compatible client
             |
       optional New API
             |
          Capy2API
             |
    Clerk JWT + Capy web API
             |
     ad-supported web model
```

每个请求创建独立网页线程，不复用其他用户或其他请求的会话历史。系统消息、对话历史、工具定义和工具结果会转换为一条网页 agent 消息，因此没有原生 chat role 的隔离保证。

工具模式下，模型输出带请求标识的 JSON 决策，桥接层验证后转换为标准 `tool_calls`。Capy 云端工具事件不会直接映射为客户端工具调用，桥接容器也不会执行下游要求的终端命令。

## Docker 部署

需要 Docker Engine 和 Docker Compose。默认仅绑定本机回环地址，不直接暴露公网端口。

```sh
git clone https://github.com/miludeerforest/Capy2API.git
cd Capy2API
cp .env.example .env
chmod 600 .env

# 生成代理密钥，写入 .env 的 CAPY_PROXY_KEY；不要公开该值。
openssl rand -hex 32

# 编辑 .env，配置代理密钥和自己的网页会话凭据。
docker compose up -d --build --wait
curl -s http://127.0.0.1:18099/healthz
```

也可用 Python 生成密钥：

```sh
python -c "import secrets; print(secrets.token_urlsafe(36))"
```

`/healthz` 只证明进程存活，**不证明登录、模型资格、推理或费用状态正常**。容器重建后仍通过 `./data:/data` 保留会话状态。

默认 Compose 使用项目自己的网络。如果下游在另一个 Docker 项目中，需要显式配置共同网络，或通过受控的宿主机入口访问。不要把另一个容器中的 `127.0.0.1` 当作宿主机地址。接入已有部署时，应保留该环境自己的网络覆盖配置，不要盲目替换生产 Compose。

## 配置

完整示例见 [`.env.example`](.env.example)。

| 配置项 | 用途 |
|---|---|
| `CAPY_PROXY_KEY` | 代理认证密钥，至少 32 字符；不能为空或使用示例弱密钥 |
| `CAPY_COOKIE` | 发往 Clerk 的 cookie header，例如 `__client=<your-cookie>` |
| `CAPY_CLERK_SESSION_ID` | 可选的 Clerk session ID；留空时发现 active session |
| `CAPY_BEARER` | 静态短期 JWT，仅适合临时验证，不能自行续期 |
| `CAPY_ORG_ID` | 组织 ID；留空时选择账号的第一个组织 |
| `CAPY_PROJECT_ID` | 可选的同组织项目 ID；留空时使用所选组织的默认项目 |
| `CAPY_ONLY_FREE` | 必须为 `1`；设为 `0` 不开启付费，而是拒绝推理 |
| `CAPY_DELETE_AFTER` | 是否删除本请求创建的线程，默认 `1` |
| `CAPY_TURN_TIMEOUT` | 上游回合超时秒数，示例为 `240` |
| `CAPY_POLL_INTERVAL` | REST 降级轮询间隔，示例为 `1.0` 秒 |
| `CAPY_REALTIME` | 是否使用实时通道，默认 `1` |
| `CAPY_RECONCILE_INTERVAL` | 实时连接期间的 REST 对账间隔，默认 `3` 秒 |
| `CAPY_HEARTBEAT_INTERVAL` | SSE 注释心跳间隔，默认 `5` 秒 |
| `CAPY_REASONING` | 未显式请求时的思考级别；留空使用模型默认值 |
| `CAPY_STATE_PATH` | 状态文件，默认 `/data/state.json` |

`.env` 修改后需重新创建容器才能生效。不要运行多个 worker 或多个实例共享同一状态文件。

### 获取和更新会话

1. 使用自己的账号登录 Capy。
2. 在浏览器实际发往 `clerk.capy.ai` 的请求中确认 `__client` cookie；不要假定它一定存放在 `capy.ai` 域下。
3. 在 `.env` 填写 `CAPY_COOKIE=__client=<your-cookie>`，或通过认证后的 `POST /admin/ingest` 导入。

导入 JSON 的结构如下；示例值不是有效凭据：

```json
{
  "cookie": "__client=<your-cookie>",
  "org_id": "<your-organization-id>"
}
```

也可单独提交 `bearer`，不要同时提交 cookie 和 bearer。导入 cookie 会清除旧静态 bearer；导入 bearer 会清除旧 cookie。导入先保存状态，再验证模型目录，因此验证失败时仍可能已保存新值。

Cookie 会因会话过期、退出登录或撤销而失效，不是永久授权。环境变量优先于持久化文件；若使用 ingest 管理会话，避免在环境中保留会覆盖它的旧凭据。

## OpenAI 兼容接口

除 `/healthz` 外，所有接口均要求：

```http
Authorization: Bearer <your-proxy-key>
```

| 接口 | 说明 |
|---|---|
| `GET /healthz` | 进程存活检查 |
| `GET /v1/models` | 当前组织通过安全筛选的模型列表 |
| `POST /v1/chat/completions` | 文本、工具调用及 SSE 响应 |
| `GET /admin/status` | 实时验证组织模型目录 |
| `GET /admin/metrics` | 最近 30 次请求的阶段指标与最终清理状态；内存记录，重启清空 |
| `POST /admin/ingest` | 导入自己的网页会话凭据 |
| `GET /admin/probe?path=...` | 固定上游、只读白名单诊断；拒绝外部 URL 和写方法 |

客户端 SDK 的 Base URL 通常为 `http://127.0.0.1:18099/v1`。

### 查询模型

先将自己的代理密钥安全注入当前 shell，不要开启 shell trace：

```sh
curl -s http://127.0.0.1:18099/v1/models \
  -H "Authorization: Bearer ${CAPY_PROXY_KEY}"
```

开发期间验证过的广告模型 ID 包括：

- `deepseek/deepseek-v4.1-flash-ads`
- `zai/glm-5.3-flash-ads`

**这些不是固定可用清单。**以账号当前 `/v1/models` 返回值为准；普通同名模型不会自动映射成广告模型。

### 文本请求

```sh
curl -s http://127.0.0.1:18099/v1/chat/completions \
  -H "Authorization: Bearer ${CAPY_PROXY_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek/deepseek-v4.1-flash-ads","reasoning_effort":"low","messages":[{"role":"user","content":"用一句话解释 HTTP 302。不要使用工具。"}]}'
```

流式请求添加 `"stream": true`，需要 usage 时添加 `"stream_options": {"include_usage": true}`。

## 接入 New API

创建 OpenAI 兼容渠道，例如命名为 `capy2api`：

1. Base URL 填写 New API 容器可以访问的桥接地址；若双方共享 Docker 网络，可使用 `http://capy2api:8080`，通常不附加 `/v1`。
2. 渠道密钥填写 `CAPY_PROXY_KEY`，**不是 Clerk cookie 或 JWT**。
3. 仅配置桥接当前列出的广告模型；可自行设置对外别名并映射回精确的 `-ads` ID。
4. 配置独立、明确的路由，避免别名与其他付费渠道混用或失败后回退。
5. 审查网关自己的模型计费规则。上游零价格不代表 New API 不扣内部额度。

桥接不支持输出 token 上限。New API 自带渠道测试可能附带 `max_tokens`，直接透传会被拒绝。如果接受此限制，可仅在该渠道的参数覆盖中移除这两个字段：

```json
{
  "operations": [
    {"mode": "delete", "path": "max_tokens"},
    {"mode": "delete", "path": "max_completion_tokens"}
  ]
}
```

这只是移除不支持的字段，**不是执行输出限制**。不要为了让测试通过而删除 `tools` 或伪造工具调用能力。

## 接入 Pi

复用现有网关 provider，或在 Pi 用户目录的 `models.json` 中配置独立 provider。以下 URL 和密钥引用均为占位示例：

```json
{
  "providers": {
    "capy-gateway": {
      "baseUrl": "https://gateway.example.com/v1",
      "api": "openai-completions",
      "apiKey": "${NEW_API_KEY}",
      "models": [
        {
          "id": "deepseek/deepseek-v4.1-flash-ads",
          "input": ["text"],
          "reasoning": true,
          "contextWindow": 900000,
          "maxTokens": 65536,
          "compat": {
            "supportsStore": false,
            "supportsDeveloperRole": false,
            "supportsReasoningEffort": true,
            "supportsUsageInStreaming": true,
            "supportsStrictMode": false,
            "thinkingFormat": "openai",
            "maxTokensField": "max_tokens"
          }
        }
      ]
    }
  }
}
```

使用 New API 别名时，把 `id` 改为网关中的名称。输出上限仅为客户端元数据，网关需按上一节处理不支持的字段。上下文值是保守配置示例，不代表所有工具列表和历史都能被桥接接受；序列化提示还受字符数上限限制。

打开 `/model` 选择模型即可重新加载模型配置。是否提供某个工具，取决于 Pi 实际注册、启用和暴露的工具列表，不由 New API 渠道名称决定。

### 工具兼容范围

支持 `tools`、`tool_choice`（auto/none/required/指定函数）、`parallel_tool_calls`、assistant `tool_calls` 历史、`tool_call_id` 结果匹配、空工具结果、SSE 工具调用与 usage。

- 工具必须由客户端执行；标准 function tools 通常无需为每个插件单独适配。
- 工具调用在完整 JSON、请求标识、工具名称和基本参数校验通过后才发送，绝不从普通文本推测调用。
- `auto/none` 下普通文本可作为回答；强制工具模式不能用普通回答代替调用。
- 参数校验只覆盖常见 JSON Schema 约束；客户端完整校验及危险操作确认仍不可省略。
- 每个决策最多 16 个调用、256 个工具定义；提示上限为 200 万字符，超过时需要压缩会话。
- 已验证本机读取、编辑、终端执行和 MCP 文本结果回传，但不是所有插件的完整兼容保证。

图片不能直接交给此文本桥接。可由客户端已有的 OCR/视觉工具先处理图片，再把文字结果回传；不要据此将模型声明为原生支持 `image`。外部视觉模型可能另行计费。

## 实时输出与性能

实时驱动使用网页 WebSocket 的 `llm-event`，只订阅当前请求线程。在有效决策就绪后尽早返回，后台尝试中断和删除本次线程。WebSocket 不可用时回退轮询，不重试创建或切换模型。

- 最终文本在可识别时增量转发，不输出推理内容；跨 chunk 转义和 Unicode 会被保留。
- **不承诺每条回复逐 token 输出**：上游可能合并整个 `message_user` 参数，因此只有一个可见内容块。
- 工具参数缓冲至完整校验通过；不会为降低首字时间而执行不完整调用。
- SSE 注释心跳只维持连接，不是可见首字；网关是否转发注释取决于其配置。
- 项目 ID 按组织短期缓存，但每次请求仍重新检查免费模型资格。
- 提示使用紧凑 JSON，不删除系统指令、历史、代码空白或工具结果。
- 不复用上游线程，避免跨请求历史串用及额外机器驻留风险。

开发期间的同提示小样本对照（low 思考，轮询／实时各 2 次）中，驱动响应就绪中位数约为 **6.02 秒／3.77 秒**。该测量不包含调用前的目录查询或公网客户端开销，不代表统计显著性、p95 或全量请求保证。输入 usage 几乎相同，不能声称明显减少模型输入。

### 阶段指标

带代理认证访问 `/admin/metrics`。非流式回复也包含 `capy` 诊断快照；最终清理结果以 metrics 为准。

| 字段 | 含义 |
|---|---|
| `prepare_ms`、`queue_ms` | 请求整理与串行排队 |
| `auth_ms`、`catalog_ms`、`project_ms` | 认证、免费目录和项目解析 |
| `ws_connect_ms`、`create_ms` | 实时握手与创建线程 |
| `first_upstream_ms` | 首个上游活动，可能只是回合开始或推理，不等于首字 |
| `first_content_ms`、`decision_ms` | 内容／完整决策就绪；仍不等于客户端实际接收时间 |
| `cleanup_ms`、`total_ms`、`cleanup` | 响应后的清理和总占用 |
| `history_chars`、`tool_schema_chars`、`prompt_chars` | 字符数，不是精确 tokenizer 计数 |
| `assistant_steps`、`upstream_steps`、`upstream_usage` | 已观测的每步与累计 usage |
| `usage_source` | 实时步骤或已提交 REST 事件 |

Usage 包含 Capy 自带上下文；多个步骤的累计输入不能直接等同于客户端单次输入。实时与 REST 的缓存字段会归一化，避免重复计算 `step-finish` 和 `finish`。提前结束时可能尚未包含中断到达前的尾部工作。

## 错误处理

- 认证失败、无资格模型、非法请求在调用前尽可能拒绝。
- 非流式工具协议错误使用 HTTP 422 和 `invalid_model_tool_decision`；回合超时使用 504。
- 流式响应开始后 HTTP 状态可能已为 200，后续失败通过 SSE `error` 帧报告；不能只检查状态码。
- 某些网关会在错误帧后补 `[DONE]`，**不能只凭 `[DONE]` 判断成功**。
- 响应就绪后清理异步进行，`capy.cleanup` 可能为 pending。清理任务仍持有凭据锁；下一请求可能因此短暂排队。
- 客户端断连会取消生成并尝试清理。中断／删除有超时且可能失败，不等于已验证云机器停止。
- 不自动重放失败请求，避免重复创建线程、重复生成或消耗额度。

## 安全与限制

1. **费用保证有限。**广告标记、零价格目录和测试时点账单不能排除延迟结算、上游变更或独立云机器费用。若要求绝对零费用，本项目不能提供该保证。
2. **保护会话。**`.env`、`data/`、日志、抓包和备份可能包含敏感信息，不得提交到 Git。状态文件权限设为 `0600`；Docker 构建上下文只允许应用源码和构建配方。
3. **管理接口使用同一密钥。**不要把代理密钥分发给不可信调用方；同 Docker 网络的容器仍可能访问服务端口。
4. **不是云端沙箱。**发现非 `message_user` 的云工具事件会中断，但无法追溯阻止已经发生的动作。不要使用含重要仓库、文件、集成或密钥的项目。
5. **有限 API 兼容。**不支持原生图片、严格结构化输出、采样参数、输出 token 限制等。文本模拟 function calling 的可靠性不能等同原生工具模型。
6. **单进程串行。**不要自行增加 worker 或共享状态文件的实例数；长回合分页和复杂工作流未全面验证。
7. **遵守服务规则。**不伪造广告曝光或地区；能请求成功不代表服务方允许第三方桥接，使用前应自行核实条款与账号权限。

## 项目结构

```text
app/capy.py       网页会话、Clerk 认证、持久化及固定上游传输
app/protocol.py   对话与 function tools 的文本协议适配
app/engine.py     实时事件、轮询降级、增量文本及线程清理
app/main.py       OpenAI 兼容端点、认证和诊断接口
Dockerfile        容器镜像
.env.example      无凭据配置示例
```

官方公开 API 与价格资料：<https://docs.capy.ai/api-reference/authentication>、<https://docs.capy.ai/models-and-pricing>。公开 API 定价不能替代网页广告模型的资格判断。
