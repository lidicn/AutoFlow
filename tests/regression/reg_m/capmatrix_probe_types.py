# -*- coding: utf-8 -*-
"""带外探针：RAW_NODE_ALLOWED 白名单 × Node-RED 运行时注册表 真实比对。

为什么必须带外做（而不是塞进 capmatrix flow 里断言）：
    Node-RED 对「未注册类型」的处理是**整个 tab 拒绝启动**，不是单节点降级。
    只要 flow 里有一个类型缺失的节点，NR 打印
        Waiting for missing types to be registered: - <type>
    然后该 tab 内所有节点（含 inject）都不注册，触发端点静默返回 404。
    也就是说：幽灵类型无法在流内被观测，只能在部署前用 GET /nodes 比对。

因此本探针本身就是 round3 的一条真实断言，且能长期防回归：
    任何人往 RAW_NODE_ALLOWED 里加一个目标运行时不存在的类型，这里立刻红。

用法（凭据走环境变量 NR_URL / NR_USER / NR_PASS）：
    python capmatrix_probe_types.py                 # 默认探 NR_URL
    python capmatrix_probe_types.py --url http://<NAS-IP>:1990
    python capmatrix_probe_types.py --url http://<NAS-IP>:1880 --json out.json

退出码：0=白名单全部可用；1=存在幽灵类型（缺失）；2=连接/鉴权失败。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
NR_SKILL = pathlib.Path(
    os.environ.get(
        "NR_SKILL_SCRIPTS",
        pathlib.Path.home() / ".workbuddy" / "skills" / "node-red-Kai-Dai" / "scripts",
    )
)


def load_whitelist() -> tuple[set[str], set[str]]:
    """从网关源码读白名单（唯一真源，避免探针与产品代码漂移）。

    Returns:
        (RAW_NODE_ALLOWED, RAW_NODE_FORBIDDEN)

    Raises:
        RuntimeError: 无法导入 dsl_engine 时抛出。
    """
    sys.path.insert(0, str(REPO / "src"))
    try:
        from autoflow_gateway import dsl_engine  # type: ignore
    except Exception as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("无法导入 autoflow_gateway.dsl_engine: %s" % exc) from exc
    return set(dsl_engine.RAW_NODE_ALLOWED), set(dsl_engine.RAW_NODE_FORBIDDEN)


def fetch_runtime_types(url: str | None) -> dict[str, str]:
    """拉取 NR 实例已注册的节点类型 → {type: module_id}。

    Args:
        url: NR 基址（None 则用环境变量 NR_URL）。

    Returns:
        类型名到提供模块 id 的映射。

    Raises:
        RuntimeError: 鉴权或请求失败。
    """
    sys.path.insert(0, str(NR_SKILL))
    if url:
        os.environ["NR_URL"] = url
    import nr_client  # type: ignore

    nr = nr_client.NodeRedClient()
    nr._ensure_auth()
    # /nodes 默认返回 HTML 描述页，必须显式要 JSON
    nr._session_headers["Accept"] = "application/json"
    mods = nr._json("GET", "/nodes")
    out: dict[str, str] = {}
    for mod in mods:
        for t in mod.get("types") or []:
            out[t] = mod.get("id", "?")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="白名单 × NR 运行时注册表 比对")
    ap.add_argument("--url", default=None, help="NR 基址，如 http://<NAS-IP>:1990")
    ap.add_argument("--json", default=None, help="把结果落盘为 JSON")
    args = ap.parse_args()

    allowed, forbidden = load_whitelist()
    try:
        runtime = fetch_runtime_types(args.url)
    except Exception as exc:
        print("[FAIL] 无法读取运行时节点表: %s" % exc)
        return 2

    base = os.environ.get("NR_URL", "?")
    missing = sorted(t for t in allowed if t not in runtime)
    present = sorted(t for t in allowed if t in runtime)
    # 铁律复核：禁止类型确实存在于运行时（说明「禁止」不是因为它不存在，而是主动封杀）
    forbidden_live = sorted(t for t in forbidden if t in runtime)

    print("=== RAW_NODE_ALLOWED × %s ===" % base)
    print("白名单 %d 类 / 运行时 %d 类" % (len(allowed), len(runtime)))
    for t in present:
        print("  [OK]      %-22s <- %s" % (t, runtime[t]))
    for t in missing:
        print("  [GHOST]   %-22s <- 运行时查无此类型" % t)
    print("禁止类型在运行时存在（应为真，证明是主动封杀）: %s" % forbidden_live)

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                {
                    "url": base,
                    "allowed_total": len(allowed),
                    "runtime_total": len(runtime),
                    "present": present,
                    "missing": missing,
                    "forbidden_live": forbidden_live,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print("已落盘:", args.json)

    if missing:
        print()
        print("[FAIL] 幽灵类型 %d 个: %s" % (len(missing), missing))
        print("       后果：含此类节点的 flow 会让整个 tab 拒绝启动（inject 触发 404）。")
        return 1
    print()
    print("[PASS] 白名单全部可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
