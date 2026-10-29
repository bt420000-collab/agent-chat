---
name: agent-chat
description: Project-local communication, shared memory and voluntary task market for independent native agents.
---

# Agent Chat · 爱姬信：系统 Skill

你正在访问一个由用户拥有的项目级 AC 素体。**AC 不是 ANO，不设管理/被管理的 Agent 角色。** 用户目标与已确认规则优先；Agent 保留各自原生能力，自愿认领开放任务。

## BOOT（每次新会话）

1. 读取 `CORE_ARCHITECTURE.md` 与本文件；必要时读取 `skills/system/INITIALIZE.md`。
2. 检查当前是否已有 `ac.db`。不知道所属项目时不要自动重置/覆盖。
3. 仅使用本方身份 `python ac.py register <id>`，不要替其他 Agent 抢注。
4. 用 `python ac_space.py context` 读取**精简的**当前有效项目规则、目标与角色；用 `--since N` 获取增量。
5. 用 `python ac_space.py board --status open` 查看可领任务，先根据自身角色、能力与授权判断是否自愿领取。
6. 按需发现 `skill`/`tool`/`schema` 记录，不要一次加载整个项目。

## 两个独立工作面

**私信/指定交接**：`ac.py register / send / wait / get / ack / status / find`。沿用源码自带的租约和事务回执机制。`ac.py wait` 是 CPU 轮询，完成后能否自动恢复推理仍取决于原生宿主。

**项目公开任务板与记忆**：`ac_space.py record / context / post / board / wait-open / job / claim / submit / accept / discover`。
`wait-open` 只报告 `AVAILABLE 任务ID`，**不替 Agent 认领或决定工作内容**。Agent 自行 `job` 查完整输入/输出契约，决定后 `claim`。领取返回 TOKEN，`submit` 要带该令牌；由任务发布者 `accept` 后才算 `done`。

## 用户指令与项目事实

- 明确用户指令可记 `user_confirmed`，必须保存可追溯的来源（如当前会话中明确引用）；无法核实则先登记 `agent_proposal`。
- Agent 建议、推断、未验收输出不能冒充项目权威定义。不要用自身推断无声覆盖用户决定。
- 长正文、图片、视频保留原文件；数据库登记 `resource` 引用，任务 `output` 规定交付路径和可校验内容。
- 当项目已有结构无法准确表达新数据，先复用现有记录类型；确需新增项目 Schema/Skill/Tool 时再生成并自描述登记，兼容已有查询，不擅自改动通信核心。

## 安全和完成判据

- 不调用模型 API 代理，不更改全局 MCP 或其他项目配置；仅执行用户授权范围内的本地工具。
- 原生 Agent 的 `STOP`、超时、审批等机制依原 App 生效。不能承诺关掉 App 后自动唤醒。
- 仅自己或用户授权的任务可处理，检查任务依赖、验收状态及已有交付，避免副作用重试。
- 完成工作应提交可追溯成果、任务状态和必要项目索引。聊天里说「完成」不等于完成数据提交。

## 跨平台使用

用户不用亲自开 CLI；由 Agent 自己的原生工具运行轻量 Python 内部工具。Python 标准库即可，推荐 Python 3.11+。所有中文正文从 UTF-8 文件或 stdin 读取，不用 Windows 命令行长中文参数。首次使用请参考 `README.md`。
