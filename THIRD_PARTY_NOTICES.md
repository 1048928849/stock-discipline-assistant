# 第三方组件与许可证

项目通过 Python 包管理器安装以下主要组件，不直接复制其源代码。实际安装的小版本可用 `python -m pip freeze` 查看。

| 组件 | 约束版本 | 许可证 | 用途 |
|---|---|---|---|
| FastAPI | 0.115.x | MIT | Web API |
| SQLAlchemy | 2.0.x | MIT | ORM |
| Alembic | 1.x | MIT | 数据库迁移 |
| Uvicorn | 0.x | BSD-3-Clause | ASGI 服务 |
| AKShare | 1.18.x | MIT | A 股公开市场数据 |
| twscrape | 0.19.x | MIT | X 公开内容采集 |
| backtesting.py | 0.6.x | AGPL-3.0 | 策略回测 |
| APScheduler | 3.x | MIT | 定时任务 |
| pandas | 2.x | BSD-3-Clause | 数据转换 |
| pandas-ta-classic | 0.3.59 | MIT | 技术指标计算 |
| vectorbt | 0.28.1 | Apache-2.0 | 逐信号向量化历史回测 |

特别说明：backtesting.py 使用 AGPL-3.0。将包含该依赖的应用对外分发或作为网络服务提供时，应由发布者核实并履行对应的源代码提供、许可证通知等义务。项目交付包含本项目完整源代码，但这不替代正式法律审查。
