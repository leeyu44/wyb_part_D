"""评测过程统一日志。

长评测（真智能体单轮小时级）中断后 print 无从追溯——runner/传输层/判卷
三层都走 logging，CLI --verbose 调到 DEBUG。窗口模式 exe 由 cli._ensure_streams
把 stderr 重定向到 exe 同级 memhall.log；本模块在其后初始化，日志随之落盘。
"""

from __future__ import annotations

import logging
import sys


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%m-%d %H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    if not verbose:
        # 三方库 INFO 噪音（paramiko 每连接数行、matplotlib 字体探测）
        for noisy in ("paramiko", "matplotlib"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
