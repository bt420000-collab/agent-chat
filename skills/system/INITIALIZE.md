# Agent Chat 初始化 Skill（通用素体）

目的：让本项目首次加入的原生 Agent 理解并使用 AC，不创建管理者 Agent。

1. 读取 `CORE_ARCHITECTURE.md` 和 `SKILL.md`，检查是否已有 `ac.db`。如果已有，默认是旧项目数据，不覆盖、不清空；确认其属于当前项目。
2. 仅问用户必要信息：项目目标是什么？是否指定 Agent/职责？是否有已确定的长期规则？允许跳过不确定项。
3. 使用 `ac.py register` 注册**自己的** Agent ID；不要代表未加入的 Agent 伪造注册与在线状态。
4. 通过 `ac_space.py record` 记录明确的项目目标与角色来源。用户未确认的信息先存 `agent_proposal`，不得冒充用户决定；提交 `user_confirmed` 需要填写确切来源。
5. 项目 Skills 和外部 Tools 只登记需要用到的入口，不自动安装或执行外部程序。
6. 新任务使用 `ac_space.py post` 开放发布，Agent 自主认领；与指定 Agent 的直接消息使用 `ac.py send/wait`。
7. 初始创建完毕后展示一小段 BOOT 摘要，停止。不要自动开始所有任务、修改项目源文件或遍历用户其它目录。

注意：数据库时间戳和工具机制为 CPU 工作；项目角色与工作方法由用户和参与 Agent 自行确定。未经授权不得修改系统级插件和外部项目。
