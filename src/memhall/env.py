"""环境配置（.env）装载的单一来源。

此前 CLI 与 UI 各手搓一份，且都用 ``v.split(" #")`` 剥行内注释——
值本身含 " #"（口令、带 fragment 的 URL）会被拦腰截断。收敛于此并修掉：
引号包裹的值原样保留，裸值才剥 " #" 起的注释。
"""

from __future__ import annotations

import os
from pathlib import Path

from memhall.paths import repo_root


def env_candidates() -> list[Path]:
    """.env 候选（先到先用，setdefault 不覆盖先读到的高优先级）：
    运行根（源码=仓库根 / exe=同级目录）→ deb 装机 ~/memhall.env。"""
    return [repo_root() / ".env", Path.home() / "memhall.env"]


def _parse_line(line: str) -> tuple[str, str] | None:
    """单行 → (key, value)；注释行/空行/无等号行返回 None。"""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, _, v = line.partition("=")
    k = k.strip()
    v = v.strip()
    if len(v) >= 2 and v[0] in ("'", '"') and v.endswith(v[0]):
        v = v[1:-1]
    else:
        i = v.find(" #")
        if i != -1:
            v = v[:i].rstrip()
    if not k:
        return None
    return k, v


def load_dotenv() -> list[Path]:
    """把候选 .env 装进进程环境（setdefault——手工 export 的值优先）。

    返回实际加载到的文件，供启动日志记录。"""
    loaded: list[Path] = []
    for env_file in env_candidates():
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            kv = _parse_line(line)
            if kv is not None:
                os.environ.setdefault(kv[0], kv[1])
        loaded.append(env_file)
    return loaded
