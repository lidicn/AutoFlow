# -*- coding: utf-8 -*-
"""部署 capmatrix 覆盖矩阵到 1990（容器 node-red-dev），只动本 tab，net-zero。

流程：读台账旧真实 id → DELETE → create_or_update_flow(占位 id) → 写回真实 id
      → trigger_inject(capm_run) → 等待 → trigger_inject(capm_dump) 落盘快照。

为什么要 DUMP：
    tail 的 join(count=N) 只要有一路用例缺席就**永不吐出**，证据文件根本不生成，
    排障时全盲。断言已双写 flow context，DUMP 旁路可随时导出当前快照，
    缺席用例会在总表里以「未返回(超时/异常)」列出。

用法：
    python capmatrix_deploy.py            # 全量：部署 + RUN + DUMP
    python capmatrix_deploy.py --dump     # 只重新 DUMP（不重新部署，不清 context）
    python capmatrix_deploy.py --run      # 只重跑 RUN + DUMP（不重新部署）

结果读取（独立于本脚本）：
    ssh <nas> 'docker exec node-red-dev cat /tmp/capm_result.txt'

注：flow 产物在 gitignored tests/fixtures_local/，NR 凭据走环境变量（NR_URL/NR_USER/NR_PASS）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
LEDGER = REPO / "tests" / "fixtures_local" / "capm_tab.id"
OUT_JSON = REPO / "tests" / "fixtures_local" / "capm_flow.json"

RUN_WAIT_S = 55  # > join timeout(45s)，让慢用例与 join 超时都有机会落地


def _client():
    sys.path.insert(0, r"C:\Users\lidicn\.workbuddy\skills\node-red-Kai-Dai\scripts")
    import nr_client  # type: ignore

    return nr_client.NodeRedClient()


def _fire(nr, node_id: str) -> None:
    """触发 inject 节点并打印 HTTP 码。

    Args:
        nr: NodeRedClient 实例。
        node_id: inject 节点 id。
    """
    try:
        code = nr.trigger_inject(node_id)
        print("触发 %s → HTTP %s" % (node_id, code))
    except Exception as exc:
        print("触发 %s 失败: %s" % (node_id, exc))


def _purge(nr, label: str) -> None:
    """清理所有同 label 的历史 tab（台账 + label 双保险）。

    坑：``POST /flow`` 由 NR 自行分配 tab id 并**忽略** body 里的 id，
    三步部署（POST 建壳 → 改 z → PUT）过程中真实 id 可能与台账记录漂移，
    只按台账 DELETE 会 404，残留 tab 一轮轮堆积、节点 id 跨 tab 重复串台。
    故先按台账删，再按 label 兜底扫一遍。

    Args:
        nr: NodeRedClient 实例。
        label: tab 显示名。
    """
    killed = set()
    if LEDGER.exists():
        old = LEDGER.read_text(encoding="utf-8").strip()
        if old:
            try:
                nr._json("DELETE", "/flow/%s" % old)
                killed.add(old)
                print("已按台账删除旧 tab:", old)
            except Exception:
                print("台账 tab 不存在（id 已漂移），转 label 兜底:", old)
    try:
        flows = nr._json("GET", "/flows")
    except Exception as exc:
        print("列举 flows 失败，跳过 label 兜底:", exc)
        return
    for n in flows:
        if n.get("type") == "tab" and n.get("label") == label and n["id"] not in killed:
            try:
                nr._json("DELETE", "/flow/%s" % n["id"])
                print("已按 label 清理残留 tab:", n["id"])
            except Exception as exc:
                print("清理残留 tab 失败 %s: %s" % (n["id"], exc))


def deploy(nr) -> str:
    """删旧 tab + 建新 tab，返回 NR 分配的真实 id。

    Returns:
        新 tab 的真实 id。
    """
    flow = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    _purge(nr, flow.get("label", ""))
    res = nr.create_or_update_flow("capm_tab", flow, force=True, allow_prod=True)
    real_id = res.get("id")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(real_id, encoding="utf-8")
    print("已部署 capmatrix tab 真实 id:", real_id)
    return real_id


def main() -> None:
    ap = argparse.ArgumentParser(description="capmatrix 部署 / 触发 / 取证")
    ap.add_argument("--dump", action="store_true", help="只触发 DUMP 落盘")
    ap.add_argument("--run", action="store_true", help="只触发 RUN + DUMP")
    args = ap.parse_args()

    nr = _client()

    if args.dump:
        _fire(nr, "capm_dump")
    elif args.run:
        _fire(nr, "capm_run")
        print("等待 %ds ..." % RUN_WAIT_S)
        time.sleep(RUN_WAIT_S)
        _fire(nr, "capm_dump")
    else:
        deploy(nr)
        _fire(nr, "capm_run")
        print("等待 %ds ..." % RUN_WAIT_S)
        time.sleep(RUN_WAIT_S)
        _fire(nr, "capm_dump")

    time.sleep(2)
    print("=== 读取结果 ===")
    print('  ssh <nas> "docker exec node-red-dev cat /tmp/capm_result.txt"')


if __name__ == "__main__":
    main()
