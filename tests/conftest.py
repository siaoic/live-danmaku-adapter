# -*- coding: utf-8 -*-
"""插件测试夹具：让宿主 1.4.x 的共享模型目录在 pytest 进程里可见。

宿主 1.4.x 把供应商/模型定义从 ``config/model_config.toml`` 搬到了
``host/model_catalog.toml``，启动 ``bot.py`` 时由宿主设置
``MAIBOT_SHARED_MODEL_CATALOG`` 指向它。pytest 进程没有这个环境变量，
直接导入 ``src.config.config`` 会在**收集阶段**因「模型列表不能为空」
抛 ValidationError，导致整个测试套件一个用例都跑不了。

这里在任何测试模块被导入之前补上环境变量。纯测试侧兼容，不修改宿主
源码，也不改任何生产配置。
"""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CATALOG = _ROOT / "host" / "model_catalog.toml"

if _CATALOG.is_file() and not os.environ.get("MAIBOT_SHARED_MODEL_CATALOG"):
    os.environ["MAIBOT_SHARED_MODEL_CATALOG"] = str(_CATALOG)
