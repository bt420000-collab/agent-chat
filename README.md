# Agent Chat（爱姬信）· 可携带素体 v0.1

**一个文件夹放进项目，让各厂商原生 Agent 在不失去个性的情况下共享项目记忆、自主接单与互相联络。**

本包是由真实小说项目的 Agent Chat 0.1.2 / schema2 通信内核剥离、通用化得到的**最小实验框架**。保留已实测的 SQLite 消息队列、原子领取、任务租约、回执与 UTF-8 处理；`ac_space.py` 提供共享记忆、公开任务板、依赖与登记接口。执行生命周期进一步把一项 Job 与它的多次具体执行 Attempt 分开记录，支持启动确认、心跳、取消请求、终态交付与显式故障恢复。**它不是新的 Agent 管理者、ANO 子模块或模型调用服务。**

架构方向见 `CORE_ARCHITECTURE.md`；实践差异与证据见 `STATUS.md`。

## DB / Skills / Tools

```text
UserProject/
  AgentChat/
    SKILL.md                  # 原生 Agent 的唯一入口
    CORE_ARCHITECTURE.md      # 长期原则
    ac.py                     # 原有通信与等待核心
    ac_space.py               # 可选：共享记忆、众包任务板
    ac.db                     # 首次调用自动生成，不随模板分发
    migrations/               # 旧库升级脚本，续接已有项目时才用
    skills/system/            # 初始化与共同协议
    skills/project/           # 项目 Skill 生长位
    tools/internal/           # 本地通信/数据库脚本说明
    tools/external/           # 外部工具登记位
    examples/                 # 不自动导入的示例
    dev/tests/                # 可选开发回归测试
```

**数据库不吞项目素材。** 大型正文、代码、图片、音频和视频仍存项目文件，AC 只保留资源引用及校验信息。不要把旧项目的 `ac.db`、日志、小说正文、账号配置和个人信息打进公开素体。

## 首次使用（由 Agent 执行，不要求用户亲自操作终端）

1. 将整个 `AgentChat/` 复制到**新的或明确授权**的项目目录中。
2. 让第一个原生 Agent 阅读 `AgentChat/SKILL.md`、`skills/system/INITIALIZE.md`，询问必要的项目目标和成员角色。
3. Agent 使用自身原生工具运行 `ac.py register <自己的id>`，随后使用 `ac_space.py record` 将用户**已确认**的项目目标、角色等登记到同一 `ac.db`；未确认的仅登记为 `agent_proposal`。
4. 其他 Agent 打开同一项目后，各自阅读 Skill、注册自身、通过 `context` 查询当前项目状态、`board` 查开放任务，依赖自身原生工具自愿领取。
5. Agent 间直接通信使用 `ac.py send / wait / get / ack`。公开任务板使用 `ac_space.py post / wait-open / claim / start / running / heartbeat / finish / accept`；执行取消与过期租约恢复分别使用 `cancel`、`recover`。两种机制共享同一个数据库；任务板不是中央派工器。

**不要直接复制原小说工程的数据库到新项目。** 本包不包含实际项目数据。新库第一次使用创建；已有库只在明确续接同一项目时打开。不同项目使用独立数据库。

## 对 Agent 的简短示范

将工作目录设置为 `AgentChat/`，所有中文长内容经 UTF-8 文件或标准输入，不使用 Windows 命令行中文长参数：

```text
python ac.py register codex --file examples/codex_profile.json
python ac.py register antigravity
python ac_space.py discover
python ac_space.py post --file examples/job_research.json
python ac_space.py board --status open
python ac_space.py wait-open --agent codex --timeout 60
python ac_space.py job DEMO-001
python ac_space.py claim DEMO-001 --agent codex
```

`claim` 返回一次性的 TOKEN，交付要使用该令牌：

```text
python ac_space.py submit DEMO-001 --agent codex --token <返回的TOKEN> --artifact outputs/DEMO-001.md
python ac_space.py accept DEMO-001 --agent codex
```

示例中 `accept` 由任务发布者确认；实际项目按用户要求设置验收流程。`examples/job_followup.json` 展示后续依赖任务，须先发布 DEMO-001。能力标签为 `research` 的任务，`wait-open` 只提醒声明具备相应能力的 Agent，Agent 仍自主决定是否 `claim`。想给其他 Agent 发具体邀请，仍用 `ac.py send`。


### 执行生命周期（新增）

公开任务板中的 Job 是一项共享工作机会；每次有人领取并实际执行，都会生成独立的 `job_attempt`。同一 Job 可以经历多次尝试，失败记录不会被下一次领取覆盖。

推荐执行顺序：

```text
post → OPEN
claim → CLAIMED（生成 attempt_id 和一次性 TOKEN）
start → STARTING（登记执行器 / runtime / session）
running → RUNNING（执行器确认已真正启动）
heartbeat → 延长执行租约
finish --result succeeded → SUBMITTED
accept → DONE
```

示例：

```bash
python ac_space.py start DEMO-001 --agent codex --token <TOKEN> --executor codex --runtime native
python ac_space.py running DEMO-001 --agent codex --token <TOKEN> --lease 3600
python ac_space.py heartbeat DEMO-001 --agent codex --token <TOKEN> --lease 3600
python ac_space.py finish DEMO-001 --agent codex --token <TOKEN> --result succeeded --artifact outputs/DEMO-001.md
python ac_space.py accept DEMO-001 --agent <发布者ID>
```

- `start` 表示执行器开始接手准备；只有确认底层运行时已启动后才调用 `running`。
- `heartbeat` 由仍在工作的执行器定期调用，续租并留下存活记录。
- `finish` 的 `succeeded` 会提交成果，但不会直接把 Job 变成 `done`；仍须由发布者验收。`failed`、`interrupted`、`start_failed` 会结束本次 Attempt 并重新开放 Job，供其他成员自愿认领；这些终态只能在执行器确认本次运行已经结束后提交。
- `cancel` 是取消请求，不代表进程已经停止。执行器确认停止后，再用 `finish --result cancelled` 结束 Attempt。
- 租约过期**不会自动重新开放任务**。可先用 `recover --action mark` 留下待核查状态；确认原执行仍活着可 `recover --action resume`，确认进程已经停止才可用 `recover --action requeue --confirmed-stopped` 或 `recover --action cancel --confirmed-stopped`（后两者也可在一次事务中完成标记与恢复）。这是为了避免旧进程仍在改文件时，另一个 Agent 又领取同一任务。
- 原有 `claim → submit` 简写仍兼容；它会被记录为一次简化的成功 Attempt。新执行器建议使用完整生命周期命令。

`job <ID>` 会返回任务详情、全部 Attempt 历史及当前执行状态。Attempt 的状态变化继续写入 AC 既有的只追加 `events` 事件表；数据库只记录进程信息，不负责替 Agent 启动或杀死外部进程。

## 共享记忆与项目结构登记

```text
python ac_space.py record --kind goal --key project_objective --agent codex --file goal.json --authority user_confirmed --evidence user:session-reference
python ac_space.py record --kind role --key image --agent codex --file role.json --authority user_confirmed --evidence user:session-reference
python ac_space.py record --kind tool --key comfyui --agent antigravity --file tool.json --authority reference
python ac_space.py context
python ac_space.py context --since 2
```

`record` 的 `--file` 要求是有效 JSON，文字用 JSON 字符串或对象表示。`context` 默认返回每个键的**当前有效投影**；`--since` 返回按修订序号追加的变化列表。后续 Agent 提案不会覆盖同键的用户已确认定义。登记 `user_confirmed` 必须写入来源引用，但**目前来源还依赖协作者如实登记，不具备密码学证明或多租户隔离**。跨项目、第三方不可信 Agent 的安全接入属于未来议题。

`kind` 允许 goal/setting/role/decision/fact/resource/skill/tool/schema/note，项目可以用自描述记录关联其自适应表结构。`ac_space.py` 的 `acs_*` 表与 `ac.py` 的 agents/messages/tasks/events 表共用数据库、互不覆盖。两组工具各有自己的任务语义：`ac.py` 的 tasks 是**定向消息接力租约**，`ac_space.py` 的 acs_jobs 是**公开众包任务板**，不能混淆。

## 关键边界

- **无中央管理 Agent。** 用户是最终项目授权来源，成员平等、自主接单，Agent 不因加入 AC 而变成子 Agent。
- **只有状态和通知，非任意客户端的万能唤醒。** 数据库监听是 CPU 工作；原生 App 是否能在收到后台任务完成通知后恢复推理，需逐客户端实测。新会话/重启后恢复未因此自动成立。
- **任务可派生且可并行。** `post` 可带 `parent_id` 和 `depends_on`，依赖任务只有在上游经发布者 `accept` 后开放。第一版采用仅引用已存在任务的 DAG 模型，防止循环。
- **显式执行恢复。** 每次公开任务领取都有独立 Attempt；租约过期不会自动重投。先标记并核查执行器是否仍存活，只有确认旧进程停止后才能重新开放或取消任务。AC 记录状态和回执，不承诺自动恢复任意原生会话或杀死外部进程。
- **DB 不是真正的身份认证。** 项目目录内的可信参与者可以使用工具；写入权限主要依赖宿主和文件系统。不得把数据库当成可公开联网的多用户服务。
- **SQLite 版本兼容。** 有相应 WAL 修复的 SQLite 使用 WAL；旧系统 SQLite 自动使用 rollback journal (`DELETE`) 以规避已知 WAL 风险。若旧 SQLite 遇到已有 WAL 数据库，先使用修补版 SQLite 安全打开/迁移，不能在运行中复制/删 WAL 文件。
- **无自动访问外部工具。** Tools 目录仅约定外部工具登记入口，真实工作流、插件、账号和密钥由项目与 Agent 自行授权管理。

## 当前验证

GitHub Actions 已在 Python 3.11 上运行完整标准库测试套件，41 项通过，覆盖通信内核、通用项目层与新增执行生命周期。它验证的是数据库协议和 CLI 状态流转，不代表已经完成真实 Codex / Antigravity 运行时的自动启动、托管和跨会话恢复测试。原小说项目报告的 20/20 原生数据库交接属于**原项目证据**，不是本可携带素体在任意 Agent 客户端上的兼容性保证。

**不要急着为 AC 加上每个行业的功能。交付共同语言，让结构随着项目实践自适应生长。**
