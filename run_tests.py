#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoFlow 测试运行器（单命令跑全套）。

用法：
    python run_tests.py              # 默认跑【离线】测试（排除需线上 NR/HA 的 live 测试）
    python run_tests.py --live       # 包含 live 测试（需 NR/HA 在线）
    python run_tests.py --all        # 同上，跑全部
    python run_tests.py dsl          # 离线基础上，只跑文件名含 "dsl" 的
    python run_tests.py --live webui# 含 live，且只跑含 "webui" 的
    python run_tests.py --smoke      # 离线全绿【硬门槛】后，可选跑 deepseek++ 黑箱冒烟
                                     #   （需 ds_bridge + 网关在线；离线则优雅跳过，不拉低门槛）
                                     #   AF_SMOKE_DRYRUN=1 只探活不点火；AF_SMOKE_SCENARIOS=1,2 指定场景

说明：每个测试文件自带 __main__ 块，会自行设置环境变量并独立运行。
本脚本只是统一调度 + 汇总，任一个失败则整体退出码非 0。

A11 · 离线 gate 硬门槛 + 可选 live smoke：
  离线全套是**硬门槛**——任一失败即退出码非 0，且【绝不】进入 --smoke 冒烟。
  --smoke 仅在离线全绿后触发，是 best-effort 的 deepseek++ 黑箱回归；ds_bridge/网关
  离线 → 跳过（视为通过，因硬门槛已达）。冒烟真跑且有场景 FAIL → 退出码非 0。

⚠️ 必须用「实际运行网关的 python」执行（系统 Python 3.13.2，已装齐
mcp/pydantic/uvicorn/starlette/httpx/dotenv）。托管 3.13.12 未装依赖，不可用。

离线/在线分类（LIVE_TESTS）：
  原 6 个「live」测试已全部离线化（mock 后端 / 本地 vhass / 进程内 TestClient /
  _FakeNR 桩），不再依赖真实 NR/HA：
    - test_gateway    ：FakeNR + FakeHA 桩，覆盖防御/意图/确认闸/目录。
    - test_staging    ：本地 ThreadingHTTPServer 起 vhass，staging 全链路。
    - test_vhass      ：本地 vhass HTTP + Store 逻辑单测。
    - test_weight_subflow：纯编译/子流程 spec 固化，无 NR/HA。
    - test_deploy_raw  ：_FakeNR + defense/state 桩，验证 remap 先于部署。
    - test_webui      ：starlette TestClient 进程内测试，仅需 starlette+mcp 框架包
                       （缺包时自动 skip，用系统 Python 3.13.2 或装齐依赖后跑全量）。
  现默认离线运行即覆盖全部 6 个；LIVE_TESTS 仅保留给未来确实需要真实实例的测试。
"""
import os
import sys
import subprocess
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(HERE, "tests")


# 需真实 NR/HA 实例才能跑的测试（默认排除，--live/--all 才包含）。
# 2026-07-13：原 6 个「live」测试已全部离线化（mock/本地 vhass/进程内 TestClient），
# 移出本集合，默认离线运行即覆盖。test_webui 仅在缺失 starlette/mcp 时自动 skip。
# 若未来新增确实要连真实实例的测试，加入本集合即可。
LIVE_TESTS = set()


def main():
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    name_filters = [a.lower() for a in args if not a.startswith("--")]
    include_live = "--live" in flags or "--all" in flags

    files = sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py")))
    if not files:
        print(f"未找到测试文件：{TESTS_DIR}")
        return 2

    selected = []
    for f in files:
        name = os.path.basename(f)
        base = name[:-3]  # 去 .py
        is_live = base in LIVE_TESTS
        if not include_live and is_live:
            continue
        if name_filters and not any(k in name.lower() for k in name_filters):
            continue
        selected.append(f)

    if not selected:
        print(f"没有匹配 {name_filters} 的测试文件（live={'包含' if include_live else '排除'}）")
        return 2

    mode = "全部(含 live)" if include_live else "离线(默认，排除 live 测试)"
    print(f"Python: {sys.executable}")
    print(f"模式: {mode}")
    print(f"测试文件数: {len(selected)}\n" + "=" * 60)

    results = []
    for f in selected:
        name = os.path.basename(f)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", f, "-q", "-p", "no:cacheprovider"],
            cwd=HERE,
            capture_output=True,
            text=True,
        )
        # 兼容旧式测试：仅以 __main__ 驱动、pytest 收集不到用例（打印
        # "no tests ran"）时，回退到 `python f` 直接运行其 __main__ 块
        # （如 unittest.main()），两路互补覆盖 pytest 风格与 __main__ 风格。
        if "no tests ran" in proc.stdout:
            proc = subprocess.run(
                [sys.executable, f],
                cwd=HERE,
                capture_output=True,
                text=True,
            )
        ok = proc.returncode == 0
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        summary = lines[-1] if lines else "(无输出)"
        results.append((name, ok, summary, proc.stdout, proc.stderr))

    # 汇总
    passed = sum(1 for _, ok, _, _, _ in results if ok)
    print("=" * 60)
    for name, ok, summary, _, _ in results:
        mark = "✅ PASS" if ok else "❌ FAIL"
        tag = " [live]" if name[:-3] in LIVE_TESTS else ""
        print(f"  {mark}  {name}{tag}")
        if not ok:
            print(f"         ↳ {summary}")
    print("=" * 60)
    print(f"总计 {len(results)} 个文件：{passed} 通过 / {len(results) - passed} 失败")
    if not include_live and LIVE_TESTS:
        left = sorted(LIVE_TESTS)
        print(f"（已排除 {len(left)} 个 live 测试；加 --live 可包含：{', '.join(left)}）")

    if passed != len(results):
        for name, ok, _, out, err in results:
            if not ok:
                print(f"\n----- {name} 失败详情 -----")
                print(out[-2000:] if out else "(无 stdout)")
                if err:
                    print("stderr:")
                    print(err[-1500:])
        # 离线硬门槛未过 → 绝不进冒烟
        if "--smoke" in flags:
            print("\n⛔ 离线门槛未通过，跳过 --smoke 冒烟（硬门槛优先）。")
        return 1
    print("🎉 全部测试通过")

    # A11 · 可选 live smoke：仅在离线全绿后触发
    if "--smoke" in flags:
        rc = _run_smoke()
        if rc != 0:
            return rc

    return 0


def _run_smoke() -> int:
    """离线全绿后跑 deepseek++ 黑箱冒烟。返回 0=通过/跳过，1=有场景 FAIL。"""
    print("\n" + "=" * 60)
    print("A11 · deepseek++ 黑箱冒烟（可选 live smoke）")
    scen_env = os.environ.get("AF_SMOKE_SCENARIOS", "").strip()
    scenarios = tuple(s.strip() for s in scen_env.split(",") if s.strip()) or ("1", "2", "3")
    if TESTS_DIR not in sys.path:
        sys.path.insert(0, TESTS_DIR)
    try:
        import smoke_deepseek
    except Exception as e:
        print(f"⏭ 冒烟模块导入失败，跳过（不影响离线门槛）：{type(e).__name__}: {e}")
        return 0
    try:
        res = smoke_deepseek.run_smoke(scenarios=scenarios)
    except Exception as e:
        print(f"⏭ 冒烟执行异常，跳过（不影响离线门槛）：{type(e).__name__}: {e}")
        return 0

    if res.get("skipped"):
        print(f"⏭ 冒烟跳过：{res.get('reason', '')}（离线硬门槛已通过 ✅）")
        probe = res.get("probe") or {}
        if probe:
            print(f"   探活：ds_bridge={'✅' if probe.get('ds_bridge') else '❌'} "
                  f"gateway={'✅' if probe.get('gateway') else '❌'}")
        return 0

    results = res.get("results", [])
    for r in results:
        mark = "✅ PASS" if r.get("ok") else "❌ FAIL"
        print(f"  {mark}  场景 {r.get('scenario')}")
    print("-" * 60)
    print(f"冒烟：{res.get('passed', 0)} 通过 / {res.get('total', 0)} 场景")
    return 0 if res.get("passed", 0) == res.get("total", 0) else 1


if __name__ == "__main__":
    sys.exit(main())
