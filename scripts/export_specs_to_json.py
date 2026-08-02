# -*- coding: utf-8 -*-
"""一次性导出脚本：把网关硬编码的 API/子流程 spec 抽成 JSON（数据/代码分离）。

用法（仓库根，PYTHONPATH=src）：
    python scripts/export_specs_to_json.py

- API_SPECS（api_specs.py 硬编码） -> src/autoflow_gateway/data/api_specs.json
- SUBFLOWS 中网关预置的子流程（demo_notify/bark_push/history×4）
  -> src/autoflow_gateway/data/subflows/subflows.json
  （不含从 API_SPECS 合并进来的 link_out 能力，避免重复）

零手抄：直接 dataclasses.asdict 序列化，保证 JSON 与代码定义 1:1。
幂等：重复运行结果一致。
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from autoflow_gateway.api_specs import API_SPECS  # noqa: E402
from autoflow_gateway.subflows import SUBFLOWS  # noqa: E402

DATA_DIR = os.path.join(ROOT, "src", "autoflow_gateway", "data")
SUBFLOW_DIR = os.path.join(DATA_DIR, "subflows")


def main() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SUBFLOW_DIR, exist_ok=True)

    # 1) API specs
    api_out = [dataclasses.asdict(s) for s in API_SPECS]
    api_path = os.path.join(DATA_DIR, "api_specs.json")
    with open(api_path, "w", encoding="utf-8") as f:
        json.dump(api_out, f, ensure_ascii=False, indent=2)

    # 2) 网关预置子流程（排除从 API_SPECS 合并进来的 link_out 能力）
    api_names = {s.name for s in API_SPECS}
    managed_keys = [
        k for k in ("demo_notify", "bark_push",
                    "history_state_at", "history_occurred",
                    "history_duration", "history_aggregate")
        if k in SUBFLOWS and k not in api_names
    ]
    sub_out = [dataclasses.asdict(SUBFLOWS[k]) for k in managed_keys]
    sub_path = os.path.join(SUBFLOW_DIR, "subflows.json")
    with open(sub_path, "w", encoding="utf-8") as f:
        json.dump(sub_out, f, ensure_ascii=False, indent=2)

    print(f"exported {len(api_out)} api specs -> {api_path}")
    print(f"exported {len(sub_out)} managed subflows -> {sub_path}")


if __name__ == "__main__":
    main()
