"""memhall —— 麟阁：面向 openKylin 生态的智能体记忆能力评测基准。

包结构（src 布局）：
  paths.py    装机布局路径解析（源码/exe/deb 四形态单源）
  cli.py      memhall 命令行入口
  discovery.py 智能体发现与环境体检（doctor）
  systests.py 系统级测试（重启/拨钟/多用户/断网）
  notify.py   UKUI 桌面通知
  adapters/   智能体适配器（契约 01；__init__ 的 ADAPTERS 为注册表）
  runner/     三阶段剧本编排
  schema/     pydantic 数据模型（契约 02/03）
  scoring/    规则判卷 + LLM judge
  report/     指标、聚合、对比与雷达图
  ui/         Web UI（FastAPI + SSE 评测直播）
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("memhall")
except PackageNotFoundError:  # 源码树裸跑（PYTHONPATH=src，未安装）
    __version__ = "dev"
