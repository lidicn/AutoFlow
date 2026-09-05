#!/usr/bin/env python3
"""v1.5.0: nr_client.py 增加 --gateway 模式（AutoFlow Pro）"""

NR = r"E:\NAS\autoflow\core\skill\scripts\nr_client.py"
with open(NR, "r", encoding="utf-8") as f:
    content = f.read()

# 1. 在 _cli() 函数开头增加网关模式检测
old_cli_start = '''def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Node-RED Admin CLI (安全增强版)")
    sp = p.add_subparsers(dest="cmd")'''

new_cli_start = '''def _gateway_cli(args):
    """AutoFlow Pro 网关模式：通过网关 REST API 操作，不直连 NR。"""
    import urllib.request
    import urllib.error

    gateway_url = os.getenv("AF_GATEWAY_URL") or _cfg.get("gateway_url") or ""
    api_key = os.getenv("AF_API_KEY") or _cfg.get("api_key") or ""
    agent_id = os.getenv("AF_AGENT_ID") or _cfg.get("agent_id") or "pro-agent"

    if not gateway_url:
        print("❌ 网关模式需要配置 gateway_url")
        print("   环境变量: AF_GATEWAY_URL")
        print("   或配置文件 ~/.autoflow-core/config.json: {\\"gateway_url\\": \\"http://host:8000\\"}")
        sys.exit(1)

    gateway_url = gateway_url.rstrip("/")

    def _api(method, path, body=None):
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
            return {"ok": False, "error": str(e)}

    cmd = args.gateway_cmd

    if cmd == "version":
        r = _api("GET", "/api/core/version")
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "health":
        r = _api("GET", "/api/core/health")
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "propose-dsl":
        dsl = args.dsl
        if not dsl and args.dsl_file:
            with open(args.dsl_file, "r", encoding="utf-8") as f:
                dsl = f.read()
        if not dsl:
            print("❌ 需要提供 DSL 文本（位置参数）或 --dsl-file")
            sys.exit(1)
        r = _api("POST", "/api/core/propose-dsl", {
            "dsl": dsl, "agent_id": agent_id,
            "preview": args.preview,
            "deploy_token": args.deploy_token or "",
        })
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
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "entities":
        qs = []
        if args.domain: qs.append(f"domain={args.domain}")
        if args.area: qs.append(f"area={args.area}")
        if args.keyword: qs.append(f"keyword={args.keyword}")
        path = "/api/core/entities" + ("?" + "&".join(qs) if qs else "")
        r = _api("GET", path)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "resolve-entity":
        r = _api("GET", f"/api/core/resolve-entity?name={args.name}&top_n={args.top_n}")
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "snapshots":
        r = _api("GET", "/api/core/snapshots")
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "rollback":
        r = _api("POST", "/api/core/rollback", {"snapshot_id": args.snapshot_id})
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "doctor":
        print("=== AutoFlow Pro 网关模式自检 ===")
        print(f"网关地址: {gateway_url}")
        print(f"API Key: {'已配置' if api_key else '未配置'}")
        print(f"Agent ID: {agent_id}")
        r = _api("GET", "/api/core/health")
        if r.get("ok"):
            print(f"✅ 网关连通，版本: {r.get('version', 'unknown')}")
        else:
            print(f"❌ 网关连接失败: {r.get('error')}")
            sys.exit(1)

    else:
        print(f"未知网关命令: {cmd}")
        sys.exit(1)


def _cli():
    # 检测 --gateway 参数（网关模式）
    if "--gateway" in sys.argv:
        import argparse
        gp = argparse.ArgumentParser(description="AutoFlow Pro 网关模式")
        gp.add_argument("--gateway", action="store_true", help="使用网关模式（不直连 NR）")
        gsp = gp.add_subparsers(dest="gateway_cmd")

        gsp.add_parser("version", help="网关版本")
        gsp.add_parser("health", help="网关健康检查")
        gsp.add_parser("doctor", help="网关模式自检")

        pd = gsp.add_parser("propose-dsl", help="★首选：提交 DSL，编译+闸门校验")
        pd.add_argument("dsl", nargs="?", default="", help="DSL 文本")
        pd.add_argument("--dsl-file", help="从文件读取 DSL")
        pd.add_argument("--preview", action="store_true", help="仅生成提案，不部署")
        pd.add_argument("--deploy-token", help="部署授权码（自动部署用）")

        dr = gsp.add_parser("deploy-raw", help="⚠️逃生舱：直接提交 raw JSON")
        dr.add_argument("--flow-file", required=True, help="flow JSON 文件路径")
        dr.add_argument("--label", help="flow 名称")
        dr.add_argument("--target", default="staging", help="部署目标 (staging/prod)")

        en = gsp.add_parser("entities", help="实体目录查询")
        en.add_argument("--domain", help="按域过滤")
        en.add_argument("--area", help="按区域过滤")
        en.add_argument("--keyword", help="关键词")

        re = gsp.add_parser("resolve-entity", help="设备名→entity_id")
        re.add_argument("name", help="设备名称")
        re.add_argument("--top-n", type=int, default=5)

        gsp.add_parser("snapshots", help="快照列表")
        rb = gsp.add_parser("rollback", help="回滚到快照")
        rb.add_argument("snapshot_id")

        gargs = gp.parse_args()
        if not gargs.gateway_cmd:
            gp.print_help()
            return
        _gateway_cli(gargs)
        return

    import argparse
    p = argparse.ArgumentParser(description="Node-RED Admin CLI (安全增强版)")
    sp = p.add_subparsers(dest="cmd")'''

if old_cli_start in content:
    content = content.replace(old_cli_start, new_cli_start, 1)
    print("1. --gateway 模式添加: OK")
else:
    print("1. --gateway 模式添加: NOT FOUND")

# 2. 更新版本号
old_ver = 'NR_CLIENT_VERSION = "3.0.2"'
new_ver = 'NR_CLIENT_VERSION = "3.1.0"  # v1.5.0: 增加 --gateway 模式（AutoFlow Pro）'
if old_ver in content:
    content = content.replace(old_ver, new_ver, 1)
    print("2. 版本号更新: OK")
else:
    print("2. 版本号更新: NOT FOUND")

with open(NR, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
