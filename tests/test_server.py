import pytest
from unittest.mock import MagicMock
from livetools.server import Daemon

def test_cmd_bp_add_success():
    daemon = Daemon("dummy_target")
    daemon.api = MagicMock()
    daemon.api.install_bp.return_value = {"ok": True, "id": 1, "msg": "BP added"}
    daemon.api.list_bps.return_value = [{"addr": 0x1000, "id": 1}]
    daemon.api.is_frozen.return_value = False

    daemon.target_name = "dummy_target"
    daemon.pid = 1234

    cmd = {"addr": 0x1000}
    resp = daemon._cmd_bp_add(cmd)

    assert resp["ok"] is True
    assert resp["bpId"] == 1
    assert resp["msg"] == "BP added"
    assert resp["target"] == "dummy_target"
    assert resp["pid"] == 1234
    assert resp["state"] == "RUNNING"
    assert resp["bpCount"] == 1

    daemon.api.install_bp.assert_called_once_with(0x1000)

def test_cmd_bp_add_failure():
    daemon = Daemon("dummy_target")
    daemon.api = MagicMock()
    daemon.api.install_bp.return_value = {"ok": False, "msg": "Invalid address"}
    daemon.api.list_bps.return_value = []
    daemon.api.is_frozen.return_value = False

    daemon.target_name = "dummy_target"
    daemon.pid = 1234

    cmd = {"addr": 0x2000}
    resp = daemon._cmd_bp_add(cmd)

    assert resp["ok"] is False
    assert resp.get("bpId") is None
    assert resp["msg"] == "Invalid address"
    assert resp["target"] == "dummy_target"
    assert resp["pid"] == 1234
    assert resp["state"] == "RUNNING"
    assert resp["bpCount"] == 0

    daemon.api.install_bp.assert_called_once_with(0x2000)

def test_cmd_bp_add_missing_key():
    daemon = Daemon("dummy_target")
    cmd = {}
    with pytest.raises(KeyError):
        daemon._cmd_bp_add(cmd)
