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
    def __init__(self, code: ErrCode, message: str, *, detail: Optional[str] = None,
                 fix: Optional[list] = None, candidates: Optional[list] = None,
                 hint: Optional[str] = None):
        # fix: 机器可直接回填的修复动作列表，元素形如 {"op": "replace", "path": "...", "value": ...}
        # candidates: 歧义时的候选 entity_id 列表（供 agent 选择后回填）
        # hint: 一句话「怎么改」（与 message 分离，便于 agent 机读）
        self.code = code
        self.message = message
        self.detail = detail or ""
        self.fix = fix or []
        self.candidates = candidates or []
        self.hint = hint or ""
        super().__init__(f"[{code.value}] {message}")

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"

    def to_dict(self) -> dict:
        """结构化出口（mimo 亮点②）：fix / candidates 让 agent 直接回填，不必重传整份 draft。"""
        return {
            "code": self.code.value,
            "message": self.message,
            "detail": self.detail,
            "fix": self.fix,
            "candidates": self.candidates,
            "hint": self.hint,
        }


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


def fix_patch(op: str, **fields) -> dict:
    """构造一个结构化修复动作（mimo 亮点②的 fix 元素）。

    例：fix_patch("resolve", entity="前门") / fix_patch("replace", path="flow.then[0].target",
                                                     value="light.living_room")
    agent 拿到后可直接回填，不必重传整份 draft。
    """
    return {"op": op, **fields}


def _compile_error_envelope(e, fix=None, candidates=None) -> dict:
    """把 DSLError 转成结构化 compile_error 信封，供 agent 机读自修正（mimo 亮点②）。

    字段：
      code   —— C_* 错误码（见 dsl_engine 常量）；非 DSLError 兜底 C_PARSE。
      line   —— 出错行号（无则 None）。
      message—— 人类可读消息（含『第 X 行:』前缀）。
      hint   —— 一句话『怎么改』，DSLError 自动从 message 的「（建议：…）」抽取。
      fix    —— 机器可直接回填的修复动作列表（无则空）。
      candidates —— 歧义时的候选 entity_id 列表（无则空）。
    任何异常类型都安全（用 getattr 兜底）。"""
    code = getattr(e, "code", "C_PARSE")
    line = getattr(e, "line", None)
    hint = getattr(e, "hint", "") or ""
    # 【v2.3.0 任务 3.1 / DCD 议题七之 C】其余错误（一次性全错反馈）。
    # dsl_engine.parse 会把后续错误挂到首个异常的 extra_errors 上；落到信封里，
    # agent 一次编译即可看到全部问题 + 各自 hint，无需「改一处编译一次」的 N 轮。
    extras = getattr(e, "extra_errors", None) or []
    all_errors = [{"code": getattr(x, "code", "C_PARSE"),
                   "line": getattr(x, "line", None),
                   "message": str(x),
                   "hint": getattr(x, "hint", "") or ""} for x in extras]
    return {"code": code, "line": line, "message": str(e), "hint": hint,
            "fix": fix or [], "candidates": candidates or [],
            "all_errors": all_errors,
            "error_count": 1 + len(all_errors)}


__all__ = ["ErrCode", "AutoFlowError", "not_found", "ambiguous_count", "forbidden",
           "demo_resolve", "fix_patch", "_compile_error_envelope"]
