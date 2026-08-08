"""F1 回归：历史子流程入参健壮 + catch↔calc 回环断路（defects-fix #73）。

离线（不触 NR / 不触 prod）执行 built.json 中 4 个 af_hist_* 子流程的**真实** NR function
节点源码：用 Node 直接 run func body（func 自带 `module.exports` 守卫，本就是为 Node 单测设计）。

覆盖工单「验证方式」的两条回归：
- Test A（缺参）：n_parse 缺时间入参 → 返回结构化错误、不抛、有限步内结束（无回环/无超时）。
- Test B（calc 异常安全）：n_err→n_calc 路径（payload=[]）永不抛 → catch↔calc 回环不可能形成。
- Test B2：_hist_input_error 置位 → n_calc 短路回显结构化错误。

注意：built.json 是 4 个历史子流程的单一真源（subflows.py 注释确认，无 build 生成器），
故直接读 built.json 即代表线上行为。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

NODE = r"C:/Users/lidicn/.workbuddy/binaries/node/versions/22.22.2/node.exe"
BUILT = (
    Path(__file__).resolve().parents[1]
    / "src/autoflow_gateway/data/subflows/nr_defs/subflows_built.json"
)

# 离线 runner：把 func 源码包成 `new Function('msg', src)`，喂入 msg 执行，回传结果。
# `module` 在 new Function 作用域内不可见 → func 内的 module.exports 守卫被安全跳过。
RUNNER = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const msg = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const fn = new Function('msg', src);
const result = fn(msg);
process.stdout.write(JSON.stringify({ msg: result }));
"""

HIST_IDS = ["af_hist_state_at", "af_hist_occurred", "af_hist_duration", "af_hist_aggregate"]


def _load_built() -> list:
    return json.loads(BUILT.read_text(encoding="utf-8"))


def _extract(built: list, sid: str, suffix: str) -> str:
    for arr in built:
        if arr and arr[0].get("type") == "subflow" and arr[0]["id"] == sid:
            for n in arr:
                if n.get("id", "").endswith(suffix):
                    return n["func"]
    raise KeyError(f"{sid}{suffix} not found in built.json")


def _run_func(func_src: str, msg: dict, tmp_path: Path) -> dict:
    func_file = tmp_path / "func.js"
    msg_file = tmp_path / "msg.json"
    runner_file = tmp_path / "runner.js"
    func_file.write_text(func_src, encoding="utf-8")
    msg_file.write_text(json.dumps(msg), encoding="utf-8")
    runner_file.write_text(RUNNER, encoding="utf-8")
    # timeout=10 兼作「无限回环」探测：若 func 真的死循环，subprocess 超时→测试失败。
    out = subprocess.run(
        [NODE, str(runner_file), str(func_file), str(msg_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if out.returncode != 0:
        raise RuntimeError(f"node failed (rc={out.returncode}): {out.stderr[:500]}")
    return json.loads(out.stdout)["msg"]


@pytest.fixture(scope="module")
def built() -> list:
    return _load_built()


@pytest.mark.parametrize("sid", HIST_IDS)
def test_a_missing_time_param_returns_structured_error(
    built: list, sid: str, tmp_path: Path
) -> None:
    """F1 Test A：缺时间入参 → n_parse 返回结构化错误、不抛、有限步结束。

    喂一个完全没有时间字段的 msg（state_at 用 `at`，其余用 `start`/`end`），
    无论该子流程要求哪个字段，都会被缺参校验捕获。
    """
    func = _extract(built, sid, "__n_parse")
    msg: dict = {"entity": "sensor.demo_temp"}  # 无任何时间字段
    result = _run_func(func, msg, tmp_path)
    assert result.get("_hist_input_error") is True, f"{sid}: n_parse 未置 _hist_input_error"
    assert result["payload"]["error"] == "missing_or_invalid_time"
    assert result["payload"]["field"] in ("start", "end", "at")


@pytest.mark.parametrize("sid", HIST_IDS)
def test_b_n_calc_empty_payload_no_throw(built: list, sid: str, tmp_path: Path) -> None:
    """F1 Test B：n_err→n_calc 路径（payload=[]）永不抛 → catch↔calc 回环不可能。

    这是 PM 标注的「真正的结构风险」：n_catch scope 含 n_calc、n_err 又把 payload=[]
    回灌 n_calc，若 n_calc 在空 payload 上抛异常即形成回环。此处断言 n_calc 在
    payload=[] 时返回结构化空答案且不抛（subprocess 超时即视为死循环/死抛）。
    """
    func = _extract(built, sid, "__n_calc")
    msg = {
        "entity": "sensor.demo_temp",
        "payload": [],
        "_hist_start": "2026-01-01T00:00:00+08:00",
        "_hist_end": "2026-01-02T00:00:00+08:00",
    }
    result = _run_func(func, msg, tmp_path)  # 抛/循环 → RuntimeError/TimeoutExpired
    assert isinstance(result, dict)
    assert "payload" in result


@pytest.mark.parametrize("sid", HIST_IDS)
def test_b2_n_calc_input_error_shortcircuit(built: list, sid: str, tmp_path: Path) -> None:
    """F1 Test B2：_hist_input_error 置位 → n_calc 短路回显结构化错误（不跑 compute*）。"""
    func = _extract(built, sid, "__n_calc")
    msg = {
        "entity": "sensor.demo_temp",
        "_hist_input_error": True,
        "_hist_input_field": "start",
        "payload": [],
    }
    result = _run_func(func, msg, tmp_path)
    assert result["payload"]["error"] == "missing_or_invalid_time"
    assert result["payload"].get("occurred") is False
    assert result["payload"].get("found") is False
