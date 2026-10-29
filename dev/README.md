# 开发测试

运行：`python -B -m unittest discover -s dev/tests -p 'test_*.py' -v`。

`test_ac.py` 与 `test_registry.py` 来自上传包的通信内核回归测试，已调整目录与本地 SQLite 安全回滚日志兼容判断；`dev/fixtures/schema1/ac.py` 是不含任何用户数据的历史 schema1 测试夹具。

`test_space.py` 是素体通用项目层独立测试，覆盖用户定义优先级、增量历史、任务依赖、领取、验收与异常拒绝。

本地环境的 SQLite 若未修补 WAL-reset 问题，工具会自动使用 SQLite rollback journal，不会冒险强行打开旧版本 WAL 数据库。项目真实部署应在原生 Codex/Antigravity 环境再次验证后台等待恢复，而非仅依赖 CPU 单元测试。
