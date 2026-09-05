#!/usr/bin/env python3
"""v1.5.6: 修复 core_version 多路径尝试"""

WEBUI = r"E:\NAS\autoflow\src\autoflow_gateway\webui.py"
with open(WEBUI, "r", encoding="utf-8") as f:
    content = f.read()

old = '''    async def core_version(request: Request):
        """网关版本 + 兼容性检查。"""
        try:
            # VERSION 在项目根目录，比 src/autoflow_gateway/ 高两级
            version_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "VERSION")
            ver = "unknown"
            if os.path.exists(version_path):
                with open(version_path, "r", encoding="utf-8") as f:
                    ver = f.read().strip()'''

new = '''    async def core_version(request: Request):
        """网关版本 + 兼容性检查。"""
        try:
            ver = "unknown"
            # 多路径尝试：容器内可能是 /app/src/... 或 /repo/src/...
            _candidates = [
                # 相对于 __file__ 的项目根（开发环境）
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "VERSION"),
                # 容器常见挂载点
                "/repo/VERSION",
                "/app/VERSION",
                # 工作目录
                os.path.join(os.getcwd(), "VERSION"),
            ]
            for _vp in _candidates:
                if os.path.exists(_vp):
                    try:
                        with open(_vp, "r", encoding="utf-8") as f:
                            ver = f.read().strip()
                        if ver and ver != "unknown":
                            break
                    except Exception:
                        continue'''

if old in content:
    content = content.replace(old, new, 1)
    print("core_version 多路径修复: OK")
else:
    print("core_version 多路径修复: NOT FOUND")

with open(WEBUI, "w", encoding="utf-8") as f:
    f.write(content)

print("Done")
