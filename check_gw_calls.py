import re
lines = open('src/autoflow_gateway/webui.py', encoding='utf-8-sig').readlines()
in_async = False
for i, line in enumerate(lines, 1):
    s = line.strip()
    if re.match(r'async def ', s):
        in_async = True
    elif re.match(r'def ', s) and 'async' not in s:
        in_async = False
    if in_async and re.search(r'gw\.\w+|_snap_mgr\(\)|tab_org\.', s):
        if 'to_thread' in s or 'asyncio' in s or '#' in s or 'str' in s.lower() or 'f"' in s or 'return' in s or 'await' in s or 'import' in s or '"' in s or "f'" in s:
            continue
        print(f'L{i}: {s[:100]}')
