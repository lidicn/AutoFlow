# -*- coding: utf-8 -*-
"""Flow 一键回滚功能测试（t_3676e6bd）。

覆盖：
1. list_flow_snapshots 正确列出所有快照
2. rollback_flow_by_snapshot 流程正常（成功/失败分支）
3. API 端点注册正确
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from autoflow_gateway import gateway as gw


def _make_snap(snap_id, flow_id, label="test_flow", ts="2026-09-05T10:00:00+00:00"):
    """构造测试用快照 JSON。"""
    return {
        "ts": ts,
        "agent_id": "test_agent",
        "kind": "apply_pre",
        "label": label,
        "ok": True,
        "flow": {
            "id": flow_id,
            "label": label,
            "nodes": [
                {"id": "n1", "type": "inject", "wires": [[]]},
                {"id": "n2", "type": "function", "wires": [[]]},
            ],
        },
    }


def _setup_test_dir(tmp_path):
    """创建测试用的快照目录结构（含日期子目录）。"""
    snap_dir = tmp_path / "flow_snapshots"
    day_dir = snap_dir / "2026-09-05"
    day_dir.mkdir(parents=True)
    return str(snap_dir)


class TestListFlowSnapshots:
    def test_empty_when_no_dir(self):
        with patch.object(gw, "_snapshot_dir", return_value="/nonexistent/path"):
            result = gw.list_flow_snapshots()
            assert result == []

    def test_returns_sorted_list(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)

        s1 = _make_snap("snap_001", "flow_a", "Flow A", ts="2026-09-05T10:00:00+00:00")
        s2 = _make_snap("snap_002", "flow_b", "Flow B", ts="2026-09-05T09:00:00+00:00")
        s3 = _make_snap("snap_003", "flow_a", "Flow A v2", ts="2026-09-05T11:00:00+00:00")

        day_dir = os.path.join(snap_dir, "2026-09-05")
        for data in [s1, s2, s3]:
            fname = f"{data['ts'].replace(':', '')}.json"
            with open(os.path.join(day_dir, fname), "w") as f:
                json.dump(data, f)

        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            result = gw.list_flow_snapshots()

        assert len(result) == 3
        # 按时间倒序
        assert result[0]["ts"] >= result[1]["ts"] >= result[2]["ts"]
        # 字段完整性
        for r in result:
            assert "snapshot_id" in r
            assert "node_count" in r
            assert r["node_count"] == 2


class TestRollbackFlowBySnapshot:
    def test_snapshot_not_found(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)
        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            result = gw.rollback_flow_by_snapshot("snap_missing", nr_client=MagicMock())
        assert result["ok"] is False
        assert "不存在" in result.get("error", "")

    def test_snapshot_without_flow_id(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)
        day_dir = os.path.join(snap_dir, "2026-09-05")
        snap_data = {"ts": "2026-09-05T10:00:00+00:00", "flow": {}}
        with open(os.path.join(day_dir, "snap_test.json"), "w") as f:
            json.dump(snap_data, f)

        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            result = gw.rollback_flow_by_snapshot("snap_test", nr_client=MagicMock())
        assert result["ok"] is False
        assert "无 flow.id" in result.get("error", "")

    def test_nr_client_required(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)
        day_dir = os.path.join(snap_dir, "2026-09-05")
        snap_data = _make_snap("snap_ok", "flow_x", "Test Flow")
        with open(os.path.join(day_dir, "snap_ok.json"), "w") as f:
            json.dump(snap_data, f)

        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            result = gw.rollback_flow_by_snapshot("snap_ok", nr_client=None)
        assert result["ok"] is False
        assert "nr_client 未传入" in result.get("error", "")

    def test_successful_rollback(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)
        day_dir = os.path.join(snap_dir, "2026-09-05")
        snap_data = _make_snap("snap_rb", "flow_abc", "ABC Flow")
        with open(os.path.join(day_dir, "snap_rb.json"), "w") as f:
            json.dump(snap_data, f)

        mock_nr = MagicMock()
        mock_nr.get_flow.return_value = {
            "id": "flow_abc",
            "nodes": [
                {"id": "n1", "type": "inject"},
                {"id": "n2", "type": "change"},
                {"id": "n3", "type": "link out"},
            ],
        }
        mock_nr.create_or_update_flow.return_value = {"ok": True, "id": "flow_abc"}

        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            with patch.object(gw, "snapshot_flow") as mock_snapshot:
                mock_snapshot.return_value = "/tmp/backup.json"
                result = gw.rollback_flow_by_snapshot("snap_rb", nr_client=mock_nr, agent_id="test_user")

        assert result["ok"] is True
        assert result["flow_id"] == "flow_abc"
        assert result["node_count_before"] == 3
        assert result["node_count_after"] == 2
        mock_nr.create_or_update_flow.assert_called_once()
        call_args = mock_nr.create_or_update_flow.call_args
        assert call_args[0][0] == "flow_abc"

    def test_backup_created_on_rollback(self, tmp_path):
        snap_dir = _setup_test_dir(tmp_path)
        day_dir = os.path.join(snap_dir, "2026-09-05")
        snap_data = _make_snap("snap_rb", "flow_y", "Y Flow")
        with open(os.path.join(day_dir, "snap_rb.json"), "w") as f:
            json.dump(snap_data, f)

        mock_nr = MagicMock()
        mock_nr.get_flow.return_value = {"id": "flow_y", "nodes": [{"id": "a"}]}
        mock_nr.create_or_update_flow.return_value = {"ok": True}

        with patch.object(gw, "_snapshot_dir", return_value=snap_dir):
            with patch.object(gw, "snapshot_flow") as mock_snapshot:
                mock_snapshot.return_value = "/tmp/backup.json"
                result = gw.rollback_flow_by_snapshot("snap_rb", nr_client=mock_nr)

        # 验证 node_count_before 存在
        assert "node_count_before" in result
        assert result["node_count_before"] == 1
        # 备份已创建
        mock_snapshot.assert_called_once()


class TestAPIEndpointRegistration:
    def test_routes_registered(self):
        """验证路由已注册。"""
        with open(os.path.join(SRC, "autoflow_gateway", "webui.py"), encoding="utf-8") as f:
            content = f.read()
        assert '"/api/flows/{flow_id}/snapshots"' in content
        assert '"/api/flows/{flow_id}/rollback"' in content
        assert "flow_snapshots_endpoint" in content
        assert "flow_rollback_endpoint" in content


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
