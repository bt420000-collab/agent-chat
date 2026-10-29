# Agent Chat（爱姬信）· 可携带素体 v0.1

**一个文件夹放进项目，让各厂商原生 Agent 在不失去个性的情况下共享项目记忆、自主接单与互相联络。**

本包是由真实小说项目的 Agent Chat 0.1.2 / schema2 通信内核剥离、通用化得到的**最小实验框架**。保留已实测的 SQLite 消息队列、原子领取、任务租约、回执与 UTF-8 处理；另增加一个隔离的最薄 `ac_space.py` 项目层，提供通用记忆、任务板、依赖与登记接口。**它不是新的 Agent 管理者、ANO 子模块或模型调用服务。**

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
5. Agent 间直接通信使用 `ac.py send / wait / get / ack`。公开任务板使用 `ac_space.py post / wait-open / claim / submit / accept`。两种机制共享同一个数据库；任务板不是中央派工器。

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
- **简化的故障处理。** 消息核心有租约与重投递；公开任务池 v0.1 在租约过期后**不自动重投**，防止重复副作用。卡住的任务需人工/Agent 根据事实核查后处理，未实现自动跨会话恢复。
- **DB 不是真正的身份认证。** 项目目录内的可信参与者可以使用工具；写入权限主要依赖宿主和文件系统。不得把数据库当成可公开联网的多用户服务。
- **SQLite 版本兼容。** 有相应 WAL 修复的 SQLite 使用 WAL；旧系统 SQLite 自动使用 rollback journal (`DELETE`) 以规避已知 WAL 风险。若旧 SQLite 遇到已有 WAL 数据库，先使用修补版 SQLite 安全打开/迁移，不能在运行中复制/删 WAL 文件。
- **无自动访问外部工具。** Tools 目录仅约定外部工具登记入口，真实工作流、插件、账号和密钥由项目与 Agent 自行授权管理。

## 当前验证

在本包独立测试中，原通信内核的若干核心操作、通用记忆与任务板用隔离数据库验证；测试日志见 `STATUS.md`。原小说项目报告的 20/20 原生数据库交接与一章返修属于**原项目证据**，不是本可携带素体在任意 Agent 客户端上的兼容性保证。

**不要急着为 AC 加上每个行业的功能。交付共同语言，让结构随着项目实践自适应生长。**
