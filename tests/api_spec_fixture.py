# -*- coding: utf-8 -*-
"""测试用 ApiSpec 临时注册器。

为什么需要（A25 工单）：
`llm_doubao_*` 四条 spec 是**产品数据**，已按用户决策从 `data/api_specs.json` 移除。
但此前多个测试把它们当作 http_api / link_out / self_use 行为的 fixture 来用 ——
产品数据一变，这些「能力回归锁」就全红，且锁的其实是「豆包还在不在」，
而不是「http_api 编译路径对不对」。

本模块提供临时注册器：测试自带一条 spec 注册进 `API_SPECS` + `SUBFLOWS`，
跑完还原。锁住的是**编译/派生行为**这个真不变量，与产品清单解耦。

用法::

    with temp_api_spec(name="t_http", kind="http_api",
                       url="http://<NAS_IP>:1880/llm/chat"):
        flow = compile_dsl(dsl, target="staging")
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoflow_gateway import api_specs as _ap  # noqa: E402
from autoflow_gateway import subflows as _sf  # noqa: E402
from autoflow_gateway.subflows import Param  # noqa: E402


def make_spec(**kwargs) -> "_ap.ApiSpec":
    """构造一条 ApiSpec，params 支持传 dict 简写（自动转 Param）。

    Args:
        **kwargs: ApiSpec 字段。`params` 可传 ``{"k": {"name": ..., ...}}``。

    Returns:
        构造好的 ApiSpec（未注册）。
    """
    params = kwargs.pop("params", None) or {}
    norm = {}
    for k, v in params.items():
        norm[k] = v if isinstance(v, Param) else Param(**v)
    return _ap.ApiSpec(params=norm, **kwargs)


@contextlib.contextmanager
def temp_api_spec(*specs, **kwargs):
    """临时把 spec 注册进 API_SPECS + SUBFLOWS，退出上下文时精确还原。

    Args:
        *specs: 已构造的 ApiSpec 对象（可多条）。
        **kwargs: 若未传 specs，则用这些字段现场构造一条（见 make_spec）。

    Yields:
        本次注册的 ApiSpec 列表。

    Raises:
        ValueError: specs 与 kwargs 同时为空。
    """
    if not specs:
        if not kwargs:
            raise ValueError("temp_api_spec 需要 specs 或构造字段之一")
        specs = (make_spec(**kwargs),)

    # 只记录「我们动过的键」，不整表快照——整表快照会把其它测试
    # 在同进程内的合法注册一并回滚，制造跨测试污染的假象。
    added_names = []
    prev_subflow = {}
    for s in specs:
        prev_subflow[s.name] = _sf.SUBFLOWS.get(s.name, _MISSING)
        _sf.SUBFLOWS[s.name] = s.to_subflow_spec()
        _ap.API_SPECS.append(s)
        added_names.append(s.name)
    try:
        yield list(specs)
    finally:
        for s in specs:
            try:
                _ap.API_SPECS.remove(s)
            except ValueError:
                pass
            old = prev_subflow.get(s.name, _MISSING)
            if old is _MISSING:
                _sf.SUBFLOWS.pop(s.name, None)
            else:
                _sf.SUBFLOWS[s.name] = old


class _Missing:
    __slots__ = ()


_MISSING = _Missing()
