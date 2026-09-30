"""审计报告「第三批」守卫（载荷瘦身，不增删工具）。

第一批（P0-1/P0-2/P1-4/list_tasks 分页）→ test_audit_first_batch.py
第二批（P1-3/P1-5/P1-7/P1-8）           → test_audit_second_batch.py
本批覆盖：
  ① autoflow_get_skill 分节取回 + max_bytes 截断（原单次回吐 56KB，审计报告 #6 / §4.2）
  ② _js_capped 出口预算（§4.2③）：必须保 JSON 合法 + 保标量 + 不超预算
  ③ ★ 元守卫：工具 docstring 里 "- 参数名：" 声明的参数必须真实存在于 schema/签名
     （实测本仓库曾有漂移：propose_dsl 的 docstring 写了签名里根本不存在的 target_tab，
      会骗 agent 传不存在的参数。本守卫即为此类漂移设卡。）

★ 纪律（MEMORY §5）：「全绿」≠「守卫有效」。接入后应做变异测试——临时把修复回退，
  确认对应用例变红且报错直指问题，再按哈希还原。
"""
import inspect
import json
import os
import re

from autoflow_gateway import mcp_server as ms


class _FakeCfg:
    def __init__(self, skills_dir):
        self.skills_dir = skills_dir


class _FakeGw:
    def __init__(self, skills_dir):
        self.cfg = _FakeCfg(skills_dir)


def _write_skill(directory: str, sections, preamble: str = "前言\n") -> str:
    """写一个带多个 ## 二级标题的 skill 文档，返回内容文本。"""
    parts = [preamble]
    for title, body in sections:
        parts.append(f"## {title}\n{body}\n")
    text = "\n".join(parts)
    os.makedirs(directory, exist_ok=True)
    p = os.path.join(directory, "probe.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return text


# ───────────────── ① get_skill 分节 + 截断 ─────────────────
def test_get_skill_truncates_and_lists_sections(tmp_path, monkeypatch):
    """默认调用应截断到 ~12KB，并给出 sections 清单与 bytes/bytes_returned。"""
    d = str(tmp_path / "skills")
    text = _write_skill(d, [("第一节", "一" * 100), ("第二节", "x" * 30000)])
    monkeypatch.setattr(ms, "_gw", lambda: _FakeGw(d))

    out = json.loads(ms.autoflow_get_skill("probe"))

    assert out["ok"] is True, out
    assert out["truncated"] is True, "56KB 级文档默认必须截断"
    assert out["bytes"] == len(text.encode("utf-8")), "bytes 保持『文档总字节』语义"
    assert out["bytes_returned"] <= 12000 + 400, out["bytes_returned"]
    assert "第一节" in out["sections"] and "第二节" in out["sections"], out["sections"]
    assert "已截断" in out["content"], "截断必须显式告知，不能静默截尾"


def test_get_skill_section_returns_only_that_section(tmp_path, monkeypatch):
    """section 命中时只回该节，不把整篇灌进上下文。"""
    d = str(tmp_path / "skills")
    _write_skill(d, [("甲节", "甲标记内容"), ("乙节", "乙标记内容")])
    monkeypatch.setattr(ms, "_gw", lambda: _FakeGw(d))

    out = json.loads(ms.autoflow_get_skill("probe", section="乙节"))

    assert out["ok"] is True, out
    assert out["section"] == "乙节", out
    assert "乙标记内容" in out["content"], out["content"]
    assert "甲标记内容" not in out["content"], "分节取回不得夹带其它节"


def test_get_skill_max_bytes_zero_returns_full(tmp_path, monkeypatch):
    """max_bytes<=0 为显式逃生舱：取全文不截断。"""
    d = str(tmp_path / "skills")
    text = _write_skill(d, [("第一节", "一" * 100), ("第二节", "x" * 30000)])
    monkeypatch.setattr(ms, "_gw", lambda: _FakeGw(d))

    out = json.loads(ms.autoflow_get_skill("probe", max_bytes=0))

    assert out["truncated"] is False, out
    assert out["bytes_returned"] == out["bytes"] == len(text.encode("utf-8")), out


def test_get_skill_unknown_section_reports_available(tmp_path, monkeypatch):
    """section 找不到时返回 ok=False 并给出可用清单，避免 agent 盲猜。"""
    d = str(tmp_path / "skills")
    _write_skill(d, [("甲节", "甲内容")])
    monkeypatch.setattr(ms, "_gw", lambda: _FakeGw(d))

    out = json.loads(ms.autoflow_get_skill("probe", section="不存在"))

    assert out["ok"] is False, out
    assert out["sections"], "应回传可用 section 清单"
    assert "甲节" in out["sections"], out["sections"]


# ───────────────── ② _js_capped 出口预算 ─────────────────
def test_js_capped_keeps_json_valid_and_preserves_scalars():
    """超预算时：仍是合法 JSON、标量字段保留、并标记 _truncated/_hint。"""
    obj = {
        "ok": True,
        "total": 999,
        "returned": 300,
        "tasks": [{"id": f"t{i}", "text": "y" * 200} for i in range(300)],
        "next": "用 claim_task 领一条",
    }
    s = ms._js_capped(obj, cap=4000)

    parsed = json.loads(s)  # 关键：绝不能吐半截字符串让客户端解析崩
    assert parsed["_truncated"] is True, parsed
    assert parsed["_hint"], "必须告知调用方如何取完整结果"
    # 标量/骨架字段一个都不能丢
    assert parsed["ok"] is True
    assert parsed["total"] == 999
    assert parsed["returned"] == 300
    assert isinstance(parsed["tasks"], str), "最大字段应被裁成提示串"
    assert len(s.encode("utf-8")) <= 4000, len(s.encode("utf-8"))


def test_js_capped_noop_when_under_budget():
    """未超预算时原样返回（零副作用）。"""
    obj = {"ok": True, "items": [1, 2, 3]}
    assert json.loads(ms._js_capped(obj, cap=65536)) == obj
    assert "_truncated" not in json.loads(ms._js_capped(obj, cap=65536))


# ───────────────── ③ 元守卫：docstring 参数漂移 ─────────────────
def test_tool_docstring_params_exist_in_schema():
    """docstring 里 '- 参数名：' 声明的参数，必须真实存在于 inputSchema 或函数签名。

    背景（实测真 bug）：autoflow_propose_dsl 的 docstring 曾写了 `target_tab` 参数及示例，
    但其签名与 inputSchema 里根本没有该参数 → agent 照 docstring 调用会传不存在的参数。
    本守卫把这类"手写文档骗调用方"的漂移钉死。

    已知例外：docstring 常以 '- resolve_entity：…' 形式**引用别的工具**，
    这类"参数名"实为工具名，跳过（否则假红）。
    """
    bullet = re.compile(r"^\s*-\s*([a-z_][a-z0-9_]*)\s*[：:]", re.M)

    def _bare(n):
        return n[len("autoflow_"):] if n.startswith("autoflow_") else n

    all_tools = []
    for srv in (ms.mcp, ms.mcp_admin):
        all_tools.extend(srv._tool_manager.list_tools())
    tool_bares = {_bare(t.name) for t in all_tools}

    bad = []
    checked = 0
    for t in all_tools:
        props = set((getattr(t, "inputSchema", {}) or {}).get("properties", {}).keys())
        try:
            sig = set(inspect.signature(t.fn).parameters)
        except Exception:
            sig = set()
        known = props | sig
        for m in bullet.finditer(t.description or ""):
            p = m.group(1)
            checked += 1
            if p in tool_bares:      # 引用的是别的工具名，不是参数
                continue
            if p not in known:
                bad.append((t.name, p))

    assert checked > 0, "未解析到任何参数条目（正则可能失配）"
    assert not bad, (
        "docstring 声明了签名/schema 中不存在的参数（漂移，会骗 agent 传错参数）："
        f"{sorted(set(bad))}")
