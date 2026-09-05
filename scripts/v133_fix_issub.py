#!/usr/bin/env python3
"""修复 _handleDeployResult 中 isSub 变量未定义的问题"""

APP = r"E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js"
with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# 1. _handleDeployResult 函数签名增加 isSub 参数
old_sig = '''async function _handleDeployResult(r, id) {
  const p = _allProposals.find((x) => x.id === id);'''

new_sig = '''async function _handleDeployResult(r, id, isSub) {
  const p = _allProposals.find((x) => x.id === id);
  if (typeof isSub === "undefined") {
    try { isSub = !!(p && (p.kind === "subflow" || JSON.parse(p.content || "{}").type === "subflow")); } catch (e) { isSub = false; }
  }'''

if old_sig in content:
    content = content.replace(old_sig, new_sig, 1)
    print("1. _handleDeployResult 增加 isSub 参数和兜底计算")
else:
    print("WARNING: 未找到 _handleDeployResult 签名")

# 2. deployProposal 中调用 _handleDeployResult 时传递 isSub
old_call1 = '''    const r = await api("POST", `/proposals/${id}/deploy`, { target: "prod" });
    return _handleDeployResult(r, id);
  };
}

// 部署结果处理（从原 deployProposal 中抽离）'''

new_call1 = '''    const r = await api("POST", `/proposals/${id}/deploy`, { target: "prod" });
    return _handleDeployResult(r, id, true);
  };
}

// 部署结果处理（从原 deployProposal 中抽离）'''

if old_call1 in content:
    content = content.replace(old_call1, new_call1, 1)
    print("2. 子流程分支调用传递 isSub=true")
else:
    print("WARNING: 未找到子流程分支调用")

old_call2 = '''    const r = await api("POST", `/proposals/${id}/deploy`, body);
    return _handleDeployResult(r, id);
  };
}'''

new_call2 = '''    const r = await api("POST", `/proposals/${id}/deploy`, body);
    return _handleDeployResult(r, id, false);
  };
}'''

if old_call2 in content:
    content = content.replace(old_call2, new_call2, 1)
    print("3. 普通部署分支调用传递 isSub=false")
else:
    print("WARNING: 未找到普通部署分支调用")

with open(APP, "w", encoding="utf-8") as f:
    f.write(content)

print("\nisSub 变量问题修复完成")
