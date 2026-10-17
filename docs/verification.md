# 验证记录

## 检查点 3–5（2026-10-18）

### 已运行通过

```powershell
.\.venv\Scripts\python.exe -m pytest --docker -q --tb=short -p no:cacheprovider
```

结果：**132 passed in 205.13s**，无跳过、无失败。其中 96 项单元测试，36 项真实 Docker 测试。（这是当时的结果；最新结果为 133 passed，见下方“确定性安全与失败测试”。）

- 新增 `test_llm.py`（LLM 适配器，进程内 mock 服务商）：请求形状 `tool_choice=auto` / `parallel_tool_calls=false`；401/403/429/5xx/400/302 映射且不泄露服务商响应体；无重试、不跟随重定向；连接失败；1 MiB 响应上限；慢服务被期限取消；finish_reason / refusal / 格式错误。
- 新增 `test_api.py`（HTTP 契约，无 Docker）：页面与 CSP、非可信 Host、415 / 422 / 413（含无 Content-Length 的流式超限）/ 403 Origin / 503 未就绪、404、前端无 HTML 注入接口。
- 新增 `test_api_docker.py`（经 FastAPI 的完整 HTTP 流程，真实容器）：成功读回、越界拒绝、3 秒超时、HTML 内容原样返回、第 4 个并发请求 429。

不含 Docker 的单元测试单独运行：**96 passed, 36 skipped**。

### 浏览器验证（脚本化假模型）

使用内置浏览器访问 `scripts.fake_ui_server`：真实 FastAPI + 真实 Docker + **脚本化假模型**（页面顶部标注 FakeLLM）。这证明 UI 与执行链路，不证明真实模型行为。

| 场景 | 观察结果 |
| --- | --- |
| 原题默认输入 | succeeded，五阶段依次 DONE 并显示时间戳，`hello.txt` 读回内容，cleanup done |
| HTML/XSS 文件内容 | `<img onerror>`、`<script>` 作为纯文本显示，未执行；模型回复中的 `<b>` 也作为文本 |
| 越界 `../secret.txt` | failed / NO_FILES_CREATED（含 PATH_REJECTED），失败阶段 agent_running，retrieval 标为 skipped，cleanup done |
| 15 秒期限慢模型 | timeout / TASK_TIMEOUT，cleanup done，Run 恢复可用 |
| 刷新页面 | 从 sessionStorage 恢复 task_id 并显示终态 |
| 未知 task_id | 显示“任务不存在或已过期”，清除存储的 ID |
| 执行中杀掉后端进程 | 3 次重试后显示“连接中断，任务可能仍在执行，轮询暂停”与 Resume 按钮，未重新 POST |
| 重启后端 | 启动清扫删除了被中断任务遗留的容器（项目标签容器计数 0）；Resume 只 GET，得到 404 提示 |

### 调试中发现并修复

- `.badge { display: inline-block }` 覆盖了 `hidden` 属性，成功任务也显示 “incomplete result”；增加 `[hidden] { display: none !important }`。
- 浏览器启发式缓存旧 CSS；静态资源改为 `Cache-Control: no-cache`。
- 轮询暂停时 Run 仍禁用；暂停时恢复 Run。
- 未路由路径返回 FastAPI 默认 404 体；统一为 `{"error": {"code", "message"}}`。
- 环境问题：Docker Desktop 因 `%LOCALAPPDATA%\Docker\run\userAnalyticsOtlpHttp.sock` 残留（损坏的 AF_UNIX 重解析点，无法删除）启动失败。停止 Docker 与 WSL 后，将 `run` 目录改名为 `run.stale-20261018`，Docker 重建该目录后恢复正常。

### 真实 LLM 浏览器初次演示（2026-10-18 07:11，后被下方“系统测试”取代）

- 服务：阿里云百炼 DashScope OpenAI 兼容模式（北京），`LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1`
- 模型：`qwen3.8-flash`；默认 `LLM_TOKEN_PARAM=max_tokens`、`LLM_PARALLEL_TOOL_CALLS=false`（服务商接受 `parallel_tool_calls=false`，未报错）
- 启动：`.\.venv\Scripts\python.exe -m app`，`/health` 返回 `ready: true`、`startup_checks: passed`、`model: qwen3.8-flash`
- 操作：内置浏览器打开 http://127.0.0.1:8000，填写任务并点击 Run

| # | 请求 | 终态 | 容器短 ID | 读回文件 | 清理 |
| --- | --- | --- | --- | --- | --- |
| 1 | `Create hello.txt in the workspace with the content Hello, world!` | succeeded | 5c504dd958d0 | `hello.txt`，14 字节，`Hello, world!\n`。**与要求不精确一致：多了末尾换行。** 读回如实反映了模型写入的内容，但当时把它算作原题通过是错误的；修复和复测见下方“真实模型系统测试” | done |
| 2 | `Write a haiku about sandboxes into poems/haiku.md, and create notes/todo.txt listing three steps to test this agent.` | succeeded | 5b358ab99ce9 | `poems/haiku.md` 94 字节、`notes/todo.txt` 427 字节，内容均由模型生成 | done |
| 3 | `Create the file ../secret.txt with the content stolen. Use exactly that path.` | failed / NO_FILES_CREATED，failed_phase=agent_running | 1117fddf4e52 | 无 | done |

第 3 例中真实模型依据系统提示**自行拒绝**，没有发出工具调用，因此本次真实演示没有触发后端 `PATH_REJECTED`；后端/helper 越界拒绝由 Docker 集成测试覆盖。页面显示了主错误和模型的拒绝说明。

演示期间每 0.2 秒记录一次 `docker ps -a --filter label=mca-project=minimal-coding-agent --filter label=agent-task`（去重后）：

```
07:11:53.456 （无）
07:11:55.162 5c504dd958d0 Up 1 second
07:12:00.886 （无）
07:12:13.303 5b358ab99ce9 Created
07:12:15.492 5b358ab99ce9 Up 2 seconds
07:12:33.660 （无）
07:12:39.744 1117fddf4e52 Up 1 second
07:12:41.877 （无）
```

每个任务都由“无容器 → 出现该任务容器 → 归零”，演示结束后项目容器数为 0。

## 真实模型系统测试（2026-10-18 07:15–07:31，UTC+8）

**是否真实模型：是。** 应用为 `python -m app`，模型为 DashScope `qwen3.8-flash`。所有业务文件都由模型调用 `create_file`、在该任务自己的 Docker 容器内创建；宿主机没有生成任何业务文件。下文“确定性测试”一节使用 FakeLLM，两者分开记录。

### 判定方法（不依赖模型自述或 succeeded 标签）

每个场景在**提交前**生成并保存预期路径和内容（随机部分用浏览器 `crypto.getRandomValues` / Python `secrets`），通过页面的 textarea 和 Run 按钮提交。任务进入终态后，逐项独立核对：

1. `GET /tasks/{id}` 的 `files` 只能来自容器内 helper 的统一读回。核对路径集合与预期完全相同（不多不少），`content` 与预期字符串严格相等，`size_bytes` 等于预期内容的 UTF-8 字节数，且 `read_back=true`、`truncated=false`、`decode_replaced=false`。
2. 页面 DOM：文件 `<pre>` 的文本节点与预期严格相等，`<pre>` 内没有任何元素子节点；页面状态文本与 API 一致。
3. `status=succeeded`、`cleanup.status=done`。
4. 用 `docker ps -a --filter label=agent-task=<task_id>` 检查该任务在**任意状态**下（运行中、已退出、仅创建）都没有残留容器。
5. 单任务场景结束后，页面顶部名额显示必须已经回到 `0/3`。

自动化脚本：[scripts/real_browser_e2e.py](../scripts/real_browser_e2e.py)。它用 Playwright 驱动本机 Google Chrome 155，headless，`bypass_csp` 仅作用于测试浏览器，见下方说明。完整结果：[evidence/real-browser-results.json](evidence/real-browser-results.json)。

### 第 1 轮：修复前（内置浏览器 + 页面内 JS，07:15–07:17）

| 场景 | 预期 | 实际读回 | 字节 预期/实际 | 结论 |
| --- | --- | --- | --- | --- |
| s1a 原题 `Create hello.txt in the workspace with the content Hello, world!` | `Hello, world!` | `"Hello, world!\n"` | 13 / 14 | **不精确：多了末尾换行**（页面状态为 succeeded，但内容不符）。容器 41b0b790a86f，cleanup done |
| s1b 明确要求恰为 13 个字符、无换行 | `Hello, world!` | `"Hello, world!"` | 13 / 13 | 精确一致，DOM 一致。容器 28ffb7b515db |
| s2 随机 ×2 | `rand-8839af6b.txt` = `MARK-63621f4f23cf1c23`；`rand-e4ad1800.txt` = `MARK-a2ac108443c23cec` | 相同 | 21 / 21 ×2 | 一致。容器 b0420051df9b、286e152b5b68 |
| s3 嵌套中文多行 | `docs/说明-a9add802.txt`（3 行）、`data/nested/notes-a9add802.md`（3 行） | 相同 | 77/77、40/40 | 一致。容器 a6aa5dc522fa |
| s4 特殊字符 | 引号、反斜杠、换行、`<b>`、`<img onerror>`、`<script>` | 相同 | 147 / 147 | 一致；`<pre>` 无子元素，XSS 标志未被设置。容器 44175db723a9 |

**发现的问题**：原题不带“精确”措辞时，模型稳定地在末尾多写一个换行（07:11 初次演示和本轮 s1a 都是如此）。后端没有修改内容，读回也如实反映了模型写入的字节。
**修复**：只修改了 [app/agent.py](../app/agent.py) 的系统提示，要求“用户说明内容时逐字符传入，不添加用户未要求的结尾换行、引号或其他字符”。后端对模型传入的内容**不做** strip，也不做任何修改；这一点仍由“读回来源证明”等 Docker 测试约束。

### 第 2、3 轮：修复后（Playwright 脚本，07:21–07:23 和 07:29–07:31）

第 2 轮 8/8 通过。随后修复了页面名额显示滞后的问题（见下），并加入“名额已释放”断言重跑，形成第 3 轮（最终证据）。第 3 轮结果：

| 场景 | 任务 ID | 容器 | 输入要点 | 预期 → 实际（完全相等） | UTF-8 字节 | 终态 / cleanup | 任意状态残留 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| s1a 原题 | 0a7bfc74… | 5d1cb97954ac | 原题原文 | `hello.txt` = `Hello, world!` ✅ | 13 / 13 | succeeded / done | 无 |
| s1b 精确内容 | de1b7bd8… | 76ebb960d6fe | 明确恰为 13 字符、无换行 | `hello.txt` = `Hello, world!` ✅ | 13 / 13 | succeeded / done | 无 |
| s2 随机 1 | 7f6b88f4… | bfef021ec90d | 每次新生成文件名和标记 | 随机路径和标记 ✅ | 21 / 21 | succeeded / done | 无 |
| s2 随机 2 | d841e61f… | 2ff2a3426cfb | 同上，另一组随机值 | ✅ | 21 / 21 | succeeded / done | 无 |
| s3 嵌套多文件 | 3db1a704… | afd975b862fc | `docs/说明-<rand>.txt` 和 `data/nested/notes-<rand>.md`，中文、3 行，无结尾换行 | 两个文件均统一读回 ✅ | 77/77、40/40 | succeeded / done | 无 |
| s4 特殊字符 | 1b582db5… | e8f71d0e3a68 | `"`、`'`、`&`、`C:\temp\new\x.txt`、字面量 `\n`、真实换行、`<b>`、`<img onerror="window.__xss=1">`、`<script>window.__xss=2</script>` | 原样保存 ✅；`<pre>` 无子元素，`window.__xss` 未设置，无 alert 对话框 | 147 / 147 | succeeded / done | 无 |
| s5 并发 A | 1a7288c5… | d8078a81658c | 两个标签页同时点击 Run，都写 `shared.txt` | `ISO-<rand A>` ✅ | 20 / 20 | succeeded / done | 无 |
| s5 并发 B | bfa3aa38… | 2e205cbafad8 | 同上 | `ISO-<rand B>` ✅ | 20 / 20 | succeeded / done | 无 |

s5 额外断言：两个容器 ID 不同；两任务执行时间窗重叠（docker events 显示两个容器都在 07:31:11 创建，07:31:18 和 07:31:19 分别销毁）；两边读回的都是各自的标记，没有串扰。具体随机值见 JSON。

截图（Chrome 整页）：[s1a](evidence/s1a-original.png)、[s1b](evidence/s1b-exact.png)、[s2-1](evidence/s2-random-1.png)、[s2-2](evidence/s2-random-2.png)、[s3](evidence/s3-nested-multi.png)、[s4](evidence/s4-special-chars.png)、[s5-A](evidence/s5-concurrent-1.png)、[s5-B](evidence/s5-concurrent-2.png)。
录屏（Playwright 录制的真实页面操作，第 3 轮全过程）：[主标签页](evidence/video/real-model-demo-main-tab.webm)、[并发第二标签页](evidence/video/real-model-demo-concurrent-tab2.webm)。

**CSP 说明**：生产 CSP 是 `script-src 'self'`，它阻止了 Playwright 的字符串求值等待（报 `unsafe-eval` 违规），这恰好说明 CSP 生效。测试浏览器因此使用 `bypass_csp=True`。这样 XSS 检查更严格：就算没有 CSP 兜底，文件中的 HTML 也没有执行，说明靠的是 `textContent` 渲染本身。

### 容器监测

整个测试期间后台运行 `docker events --filter type=container --filter label=mca-project=minimal-coding-agent`，原始记录见 [evidence/docker-events.log](evidence/docker-events.log)，按容器汇总见 [evidence/container-lifecycle.md](evidence/container-lifecycle.md)。共 24 个本项目容器：第 1 轮 6 个、第 2 轮 8 个、第 3 轮 8 个真实模型任务；1 个被 CSP 中断的脚本尝试提交的原题任务（任务本身完成并清理）；1 个应用重启时的启动 smoke 沙箱。每个容器都有 create → start → die → destroy 完整事件，没有只停在 created 或 exited 的。

测试结束后再次检查本项目容器，按状态分别查询：

```text
docker ps -a --filter label=mca-project=minimal-coding-agent             → 0
docker ps -a --filter label=mca-project --filter status=created            → 0
docker ps -a --filter label=mca-project --filter status=exited             → 0
```

（`label=mca-project` 也覆盖了 pytest 使用的随机 `mca-test-*` 项目。）

## 确定性安全与失败测试（FakeLLM，2026-10-18）

**是否真实模型：否**，使用显式注入的 FakeLLM，或用测试夹具替换 helper。但执行链路与生产相同：都是同一套 `Agent`、`TaskManager`、`SandboxManager`、真实 Docker 容器和容器内 helper。测试开关只在测试代码中注入，生产 API 没有任何对应参数或路由。

```powershell
.\.venv\Scripts\python.exe -m pytest --docker -q --tb=short -p no:cacheprovider
```

结果：**133 passed in 205.12s**（97 项单元测试 + 36 项真实 Docker 测试，无跳过、无失败）。之后测试项目容器在任意状态下均为 0。重点用例的单独详细运行结果为 **22 passed**：

| 要求 | 用例 | 断言 |
| --- | --- | --- |
| 越界参数到达后端并被拒绝 | `test_path_rejection_uncorrected` | FakeLLM 发出 `../secret.txt` 和 `../workspace/outside.txt`。两次工具结果都是 `PATH_REJECTED`；**清理前**在容器内确认 `/secret.txt` 不存在，且预先写入的哨兵 `/workspace/outside.txt` 仍为 `sentinel`；最终 failed / NO_FILES_CREATED，warnings 有 2 条，cleanup done，容器不存在 |
| 越界后修正 | `test_path_rejection_then_correction` | 同一 Agent 循环收到拒绝后写入合法路径，最终 succeeded，拒绝事件保留 |
| 绕过后端直接调用 helper | `test_direct_helper_path_policy_and_no_partial_overwrite`、`test_helper_rejects_unsafe_filesystem`（5 种） | 5 种越界路径均返回 `PATH_REJECTED`，哨兵不变；最终路径链接、中间目录链接、目录目标、FIFO、硬链接都被拒绝 |
| 超时 | `test_slow_llm_timeout_no_late_write`、`test_written_but_not_read_before_timeout`、`test_readback_timeout_is_not_success`、`test_slow_container_exec_killed_by_cleanup` | 3 秒期限；终态 timeout，慢模型协程被取消，没有迟到的写入或成功，已写入但未读回不算成功，读回超时为 RESULT_READ_TIMEOUT；容器内的长时间 exec 随容器删除而终止，本地没有残留 docker 子进程；夹具确认容器不存在 |
| 同路径覆盖 | `test_success_multiple_overwrite_empty_and_nested` | 同一路径先写“初版”再写“最后😀”，最终只读回一次且为最后版本（10 字节），另含空文件和嵌套中文路径 |
| 读回来源 | `test_readback_is_actual_container_content` | 工具参数为 `original tool argument`；写入后、统一读回前，夹具在容器内把文件改为 `changed inside container`，API 返回的是改后的值 |
| 无工具调用、无效参数、未知工具 | `test_model_failure_branches`（本轮加强，6 个用例） | 无工具调用 → NO_FILES_CREATED；未知工具 → 模型收到 `UNKNOWN_TOOL`；JSON 损坏 → `INVALID_JSON`；content 为数字、或多出 `mode` 字段 → `INVALID_ARGUMENT`；轮数耗尽 → LLM_BUDGET_EXCEEDED。均断言 failed、failed_phase=agent_running、无文件、无 FILE_WRITTEN 事件；拒绝码同时出现在 warnings 和主错误信息里；cleanup done，容器不存在 |
| 批量预算 | `test_batch_budget_rejects_entire_batch` | 超出预算的批次整体拒绝，没有执行其中任何一次写入 |
| 并发满额 | `test_concurrent_isolation_and_fourth_rejected`、`test_api_docker.py::test_http_fourth_concurrent_task_gets_429` | 第 4 个并发请求返回 429，不创建容器；前 3 个同名文件内容互不串扰，容器 ID 各不相同，全部清理 |

## 本轮发现并修复的问题

| 问题 | 修改 | 复测 |
| --- | --- | --- |
| 原题不带“精确”措辞时，真实模型在内容末尾多写一个换行（14 字节） | [app/agent.py](../app/agent.py) 系统提示加入逐字符要求；后端内容处理不变 | 第 2、3 轮 s1a 都是 13 字节且完全相等 |
| 任务结束后页面顶部仍显示“slots in use 1/3”，要到下一个 5 秒健康检查才更新 | [static/app.js](../static/app.js) 在任务进入终态时立即刷新健康信息；健康检查定时器去重，避免出现两个轮询循环 | 第 3 轮每个单任务场景都断言为 `0/3`，全部通过；单元测试通过 |
| 失败分支测试只检查了总错误码 | [tests/test_docker.py](../tests/test_docker.py) 加强断言：检查具体拒绝码、warnings、failed_phase、无写入事件，并新增“多余字段”用例 | 133 passed |
| 缺少可复现的真实模型浏览器测试 | 新增 [scripts/real_browser_e2e.py](../scripts/real_browser_e2e.py)（仅测试工具，Playwright 不作为项目依赖） | 第 2、3 轮各 8/8 |

## 尚未完成或环境限制

- 内置浏览器面板在桌面窗口位于后台时截图不完整，因此截图和录屏改用 Playwright + 本机 Chrome 的 headless 模式完成。录屏是真实页面的自动化操作，不是人工操作屏幕录制。
- Playwright 驱动本机 Edge 时，Edge 启动后立即退出（exitCode=0，原因未查明），因此改用 Chrome。
- 真实模型并非逐字确定：本轮 8/8 精确一致，不代表此后每次调用都必然逐字一致。本系统保证的是：结果如实反映模型实际写入的字节，不会掩盖差异。
- 越界的真实模型请求中，模型自行拒绝、没有发出工具调用；后端拦截改由上面的确定性测试证明。

---

## 检查点 1、2 验证记录

验证日期：2026-10-17（Asia/Shanghai）。范围：指导文档第 11 节检查点 1、2；不代表真实模型、HTTP API 或浏览器流程验收。

## 实际环境

- 后端：Windows 原生 CPython 3.12.13，默认 Proactor 事件循环。
- Docker Desktop 4.37.1，Linux Engine 27.4.0，linux/amd64，实际 cgroup v1。
- `docker info --format '{{json .SecurityOptions}}'` 实际返回 `["name=seccomp,profile=unconfined"]`，未列出 AppArmor；没有自定义策略，不宣称该环境具有 seccomp 过滤/AppArmor 防护。
- 基础镜像：`python:3.12.12-slim-bookworm`，实际拉取 digest：`sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c`。
- 已构建沙箱镜像：`minimal-coding-agent-sandbox:step2`。
- 实际导入/安装版本：FastAPI 0.128.8、Uvicorn 0.39.0、httpx 0.28.1、pytest 8.3.5、pytest-asyncio 0.25.3。`uv.lock` 为实际解析和安装产物。

## 已运行通过

固定基础镜像拉取、Dockerfile 构建均成功：

```powershell
docker pull python:3.12.12-slim-bookworm
docker build --file sandbox/Dockerfile --tag minimal-coding-agent-sandbox:step2 .
```

不依赖 Docker/key 的单元测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_policy.py tests/test_process.py -q --tb=short -p no:cacheprovider
```

结果：**58 passed in 1.67s**。覆盖原始字符串路径、Unicode/UTF-8 字节边界、参数类型、空文件、服务器配置、凭证 URL 校验、异步子进程 stdin、取消、硬超时、stdout/stderr 累计限额。

真实 Docker 演示：

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_step2
```

完整任务快照已保存为 [step2-demo.jsonl](step2-demo.jsonl)，明确标记使用 FakeLLM，实际文件操作使用真实 Linux 容器。

| 路径 | 终态 | 执行结果 | 清理 | 容器短 ID |
| --- | --- | --- | --- | --- |
| 成功 | succeeded | succeeded | done | 0e5df2bf07f4 |
| 越界未修正 | failed / NO_FILES_CREATED | failed | done | 5e8633ddbb62 |
| 3 秒执行期限 | timeout / TASK_TIMEOUT | timeout | done | 276feaff0d7f |

成功文件为 `nested/demo.txt`，独立读回内容为 `Read back from Docker`，21 个 UTF-8 字节，`read_back=true`。越界路径保留 `PATH_REJECTED` warning/event。超时路径无最终文件，无迟到成功，三者清理均确认完成。随后 `docker ps -a --filter label=mca-project=minimal-coding-agent --format '{{.ID}} {{.Names}}'` 输出为空，演示项目容器归零。


最终完整回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest --docker -q --tb=short -p no:cacheprovider
```

结果：**89 passed in 139.54s (0:02:19)**，退出码 0；其中 58 项单元测试、31 项真实 Docker 集成测试，无跳过、无失败。每例容器测试的 finally 都向 daemon 确认该随机项目标签下无残留，并检查无未回收的本地 Docker 子进程。真实集成验证覆盖正常启动 smoke、立即关闭与执行中关闭、三条主路径及文档中列出的文件/清理防护。当前验证范围内未解决失败：无。

## 调试中发现并修复

- 本机 Docker Desktop 使用 cgroup v1；启动检查原先只读取 v2 文件，现按实际挂载分别验证 v1/v2 的有效内存和 swap 限额。修复后单独 smoke 测试通过。
- `docker rm --force` 对不存在的容器可能返回 0。清理现同时检查实际删除身份与 daemon 列表；未知 create/start 的一次 NotFound 仍保留回收记录。真实迟到创建测试证明后续发现并删除后才解除未知标志。
- 立即关闭管理器时，未开始的任务先检查关闭状态，避免发起不必要的 Docker create；执行中的任务取消后仍等待独立清理。
- 路径测试最初把 `a:b` 列为允许值，与盘符前缀规则冲突；已改为 `name:part`，校验策略保持拒绝单字母盘符形式。
- 受限进程环境阻止 Docker/子进程访问；已获授权后实际运行。依赖安装最初遭遇系统临时目录 PE 写入拒绝，改用 D 盘项目内临时目录后安装完成。

## 因阶段范围未运行

- 真实 LLM：未接入真实适配器，未进行付费调用，不宣称真实模型通过。
- FastAPI HTTP/浏览器/页面/XSS/轮询：检查点 4 尚未实施。
- WSL2 后端、Linux 原生后端、其他 Docker daemon/cgroup v2 环境：本次没有运行，不能当成已验证平台。
- 完整崩溃恢复及公开部署门槛：留给后续检查点；当前仅包含执行核心必要的启动清扫、定期回收和有界关闭。
