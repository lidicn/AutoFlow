#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nr_client URL 编码回归测试：flow ID 含中文时必须 percent-encode，
否则 urllib.request 拼 URL 会抛 'ascii' codec can't encode。
运行：python tests/test_nr_url_encoding.py
"""
import sys
import tempfile
import os

sys.path.insert(0, str(__file__).replace("\\", "/").rsplit("/", 2)[0] + "/src")

# nr_client 是 vendored lib，不依赖 gateway 配置
from autoflow_gateway.lib import nr_client as NR


def test_build_url_encodes_chinese():
    cli = NR.NodeRedClient(url="http://<NAS_IP>:1880")
    # 含中文的 flow ID（DSL 引擎 _slug 会保留中文）
    ep = "/flow/af_scene_书房入户播报"
    url = cli._build_url(ep)
    assert url.isascii(), f"URL 含非 ASCII：{url}"
    assert "书房" not in url, "中文未被编码"
    assert "%E4%B9%A6" in url, "中文应按 UTF-8 编码"
    # base_url 不变
    assert url.startswith("http://<NAS_IP>:1880/flow/"), url
    print("  ✓ 中文 flow_id 正确 percent-encode")


def test_build_url_preserves_ascii_and_slashes():
    cli = NR.NodeRedClient(url="http://localhost:1880/")
    url = cli._build_url("/flow/e70a201b5f004927")
    assert url == "http://localhost:1880/flow/e70a201b5f004927", url
    print("  ✓ 纯 ASCII flow_id 行为不变")


def _run():
    test_build_url_encodes_chinese()
    test_build_url_preserves_ascii_and_slashes()
    print("\n全部测试通过 ✅  (2/2)")


if __name__ == "__main__":
    _run()
