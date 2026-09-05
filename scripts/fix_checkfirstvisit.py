f = r'E:\NAS\autoflow\src\autoflow_gateway\webui\static\app.js'
with open(f, 'r', encoding='utf-8') as fh:
    c = fh.read()

old = '  else if (tab === "update") loadUpdate();\n}'
new = '  else if (tab === "update") loadUpdate();\n  // 首次访问引导\n  if (typeof checkFirstVisit === "function") checkFirstVisit(tab);\n}'

if old in c:
    c = c.replace(old, new, 1)
    with open(f, 'w', encoding='utf-8') as fh:
        fh.write(c)
    print('OK: checkFirstVisit added')
else:
    print('NOT FOUND')
    idx = c.find('loadUpdate();')
    print(repr(c[idx:idx+60]))
