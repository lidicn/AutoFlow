#!/usr/bin/env python3
"""v1.5.8: nr_client.py 增加离线降级 + raw-to-dsl 命令"""

NR_CLIENT = r"E:\NAS\autoflow\core\skill\scripts\nr_client.py"
with open(NR_CLIENT, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 _gateway_cli 的 _api 函数中增加离线降级
old_api = '''    def _api(method, path, body=None):
        url = f"{gateway_url}{path}"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}'''

new_api = '''    def _gw_available():
        """检查网关是否可用。"""
        try:
            req = urllib.request.Request(f"{gateway_url}/api/core/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8")).get("ok", False)
        except Exception:
            return False

    def _fallback_to_direct(cmd, args):
        """网关不可用时降级到直连 NR 模式。"""
        print(f"⚠️  网关不可用，降级到直连 NR 模式（命令: {cmd}）", file=sys.stderr)
        # 构造直连模式的参数
        direct_argv = ["nr_client.py"]
        if cmd == "propose-dsl":
            # 直连模式没有 propose-dsl，提示用户
            print("❌ 直连模式不支持 propose-dsl，请确保网关可用", file=sys.stderr)
            sys.exit(1)
        elif cmd == "deploy-raw":
            direct_argv += ["deploy-raw", "--flow-file", args.flow_file]
            if args.label:
                direct_argv += ["--label", args.label]
        elif cmd == "entities":
            direct_argv += ["entities"]
        elif cmd == "snapshots":
            direct_argv += ["snapshots"]
        elif cmd == "rollback":
            direct_argv += ["rollback", args.snapshot_id]
        elif cmd in ("version", "health", "doctor"):
            print("❌ 直连模式不支持该命令", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"❌ 直连模式不支持命令: {cmd}", file=sys.stderr)
            sys.exit(1)
        # 调用直连模式
        old_argv = sys.argv
        sys.argv = direct_argv
        try:
            _cli()
        finally:
            sys.argv = old_argv

    def _api(method, path, body=None, allow_fallback=True):
        url = f"{gateway_url}{path}"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            # 网络错误时检查是否降级
            if allow_fallback and not _gw_available():
                return {"ok": False, "error": f"网关不可用: {e}", "_fallback": True}
            return {"ok": False, "error": str(e)}'''

if old_api in content:
    content = content.replace(old_api, new_api, 1)
    print("1. _api 离线降级: OK")
else:
    print("1. _api 离线降级: NOT FOUND")

# 2. 在 propose-dsl 命令后增加 raw-to-dsl 命令
old_cmd = '''    elif cmd == "deploy-raw":
        if not args.flow_file:
            print("❌ 需要 --flow-file 指定 flow JSON 文件")
            sys.exit(1)
        with open(args.flow_file, "r", encoding="utf-8") as f:
            flow_json = json.load(f)
        r = _api("POST", "/api/core/deploy-raw", {
            "flow_json": flow_json, "agent_id": agent_id,
            "label": args.label or "", "target": args.target or "staging",
        })
        print(json.dumps(r, ensure_ascii=False, indent=2))'''

new_cmd = '''    elif cmd == "raw-to-dsl":
        if not args.flow_file:
            print("❌ 需要 --flow-file 指定 flow JSON 文件")
            sys.exit(1)
        with open(args.flow_file, "r", encoding="utf-8") as f:
            flow_json = json.load(f)
        r = _api("POST", "/api/core/raw-to-dsl", {
            "flow_json": flow_json, "agent_id": agent_id,
        })
        if r.get("ok"):
            print("=== DSL 草稿（仅供参考，请根据实际需求调整）===")
            print(r.get("dsl_draft", ""))
            print()
            print(f"分析: {r.get('analysis', {})}")
        else:
            print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "deploy-raw":
        if not args.flow_file:
            print("❌ 需要 --flow-file 指定 flow JSON 文件")
            sys.exit(1)
        with open(args.flow_file, "r", encoding="utf-8") as f:
            flow_json = json.load(f)
        r = _api("POST", "/api/core/deploy-raw", {
            "flow_json": flow_json, "agent_id": agent_id,
            "label": args.label or "", "target": args.target or "staging",
        })
        # 网关不可用时降级
        if r.get("_fallback"):
            _fallback_to_direct("deploy-raw", args)
            return
        print(json.dumps(r, ensure_ascii=False, indent=2))'''

if old_cmd in content:
    content = content.replace(old_cmd, new_cmd, 1)
    print("2. raw-to-dsl 命令: OK")
else:
    print("2. raw-to-dsl 命令: NOT FOUND")

# 3. 在 argparse 中增加 raw-to-dsl 子命令
old_arg = '''        dr = gsp.add_parser("deploy-raw", help="⚠️逃生舱：直接提交 raw JSON")
        dr.add_argument("--flow-file", required=True, help="flow JSON 文件路径")
        dr.add_argument("--label", help="flow 名称")
        dr.add_argument("--target", default="staging", help="部署目标 (staging/prod)")'''

new_arg = '''        r2d = gsp.add_parser("raw-to-dsl", help="将 raw JSON 转换为 DSL 草稿")
        r2d.add_argument("--flow-file", required=True, help="flow JSON 文件路径")

        dr = gsp.add_parser("deploy-raw", help="⚠️逃生舱：直接提交 raw JSON（网关不可用时自动降级直连）")
        dr.add_argument("--flow-file", required=True, help="flow JSON 文件路径")
        dr.add_argument("--label", help="flow 名称")
        dr.add_argument("--target", default="staging", help="部署目标 (staging/prod)")'''

if old_arg in content:
    content = content.replace(old_arg, new_arg, 1)
    print("3. raw-to-dsl argparse: OK")
else:
    print("3. raw-to-dsl argparse: NOT FOUND")

# 4. 更新版本号
old_ver = '__version__ = "3.1.0"'
new_ver = '__version__ = "3.2.0"'
if old_ver in content:
    content = content.replace(old_ver, new_ver, 1)
    print("4. 版本号更新: OK")
else:
    print("4. 版本号更新: NOT FOUND")

with open(NR_CLIENT, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
