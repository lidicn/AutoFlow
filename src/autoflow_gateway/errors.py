"""C19 — 结构化错误码基座 (errors.py)

AutoFlow 统一错误码与异常基类。其余模块（gateway / mcp_server / webui）按需继承或调用，
消灭「静默 count:0」「id 不存在却不报错」等三义（C6+C7 价值点借本基座落地）。

示范接入见文件末尾 `demo_resolve()`：在真实调用点抛出结构化错误，
替代原先「存在/不存在 症状同」的模糊返回。
"""
from __future__ import annotations
from enum import Enum
from typing import Optional


class ErrCode(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    FORBIDDEN = "FORBIDDEN"
    TIMEOUT = "TIMEOUT"
    BAD_REQUEST = "BAD_REQUEST"


class AutoFlowError(Exception):
    def __init__(self, code: ErrCode, message: str, *, detail: Optional[str] = None):
        self.code = code
        self.message = message
        self.detail = detail or ""
        super().__init__(f"[{code.value}] {message}")

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


def not_found(entity: str, ident: str) -> AutoFlowError:
    """id 不存在 → 明确报错（C6 价值点）。"""
    return AutoFlowError(ErrCode.NOT_FOUND, f"{entity} {ident} 不存在", detail=ident)


def ambiguous_count() -> AutoFlowError:
    """count:0 三义基座（C7 价值点）：未触发 / 帧过 TTL / id 不存在 显式分流。"""
    return AutoFlowError(
        ErrCode.AMBIGUOUS,
        "count:0 三义：未触发 / 帧过TTL / id不存在 需显式区分（不再静默返回 count:0）",
    )


def forbidden(op: str) -> AutoFlowError:
    return AutoFlowError(ErrCode.FORBIDDEN, f"操作被铁律拒绝: {op}", detail=op)


def demo_resolve(target_id: str, live_ids: list) -> dict:
    """示范接入：真实调用点用结构化错误替代模糊返回。

    旧逻辑：「id 不在 live 则静默 count:0 返回」→ 新逻辑：显式抛出 AutoFlowError(NOT_FOUND)。
    """
    if target_id not in live_ids:
        raise not_found("flow", target_id)
    return {"ok": True, "applied": target_id}


__all__ = ["ErrCode", "AutoFlowError", "not_found", "ambiguous_count", "forbidden", "demo_resolve"]
