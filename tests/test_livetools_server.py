import pytest
import sys
from unittest.mock import MagicMock, patch, call
from pathlib import Path
from livetools.server import Daemon


def test_on_message_unknown_type_ignored(capsys):
    with patch("livetools.server.frida"):
        daemon = Daemon("dummy")

        # Unknown message type is silently ignored (no send/error key)
        daemon._on_message({"type": "unknown"}, b"")

        captured = capsys.readouterr()
        assert captured.err == ""


def test_attach_spawn():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        mock_dev.spawn.return_value = 12345
        mock_session = MagicMock()
        mock_dev.attach.return_value = mock_session

        mock_script = MagicMock()
        mock_session.create_script.return_value = mock_script

        mock_agent_js.read_text.return_value = "console.log('test');"

        daemon = Daemon("/fake/path/target.exe", spawn=True)
        daemon.attach()

        mock_frida.get_local_device.assert_called_once()
        mock_dev.spawn.assert_called_once_with(
            ["/fake/path/target.exe"], cwd=str(Path("/fake/path/target.exe").resolve().parent))
        mock_dev.attach.assert_called_once_with(12345)
        mock_session.create_script.assert_called_once_with("console.log('test');")
        mock_script.load.assert_called_once()

        assert daemon.pid == 12345
        assert daemon.target_name == "target.exe"
        assert daemon.api == mock_script.exports_sync

        assert call("detached", daemon._on_session_detached) in mock_session.on.call_args_list
        mock_script.on.assert_called_once_with("message", daemon._on_message)


def test_attach_pid():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        mock_session = MagicMock()
        mock_frida.attach.return_value = mock_session

        mock_script = MagicMock()
        mock_session.create_script.return_value = mock_script

        daemon = Daemon("12345", spawn=False)
        daemon.attach()

        mock_frida.attach.assert_called_once_with(12345)
        assert daemon.pid == 12345


def test_attach_process_name_found():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        proc1 = MagicMock()
        proc1.name = "other.exe"
        proc2 = MagicMock()
        proc2.name = "Target.exe"
        proc2.pid = 9999
        mock_dev.enumerate_processes.return_value = [proc1, proc2]

        mock_session = MagicMock()
        mock_frida.attach.return_value = mock_session

        daemon = Daemon("target.exe", spawn=False)
        daemon.attach()

        mock_frida.attach.assert_called_once_with(9999)
        assert daemon.pid == 9999
        assert daemon.target_name == "target.exe"


def test_attach_process_name_not_found():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        proc1 = MagicMock()
        proc1.name = "other.exe"
        mock_dev.enumerate_processes.return_value = [proc1]

        daemon = Daemon("target.exe", spawn=False)
        with pytest.raises(RuntimeError, match="Process 'target.exe' not found"):
            daemon.attach()


def test_attach_exception_resume_if_spawn():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        mock_dev.spawn.return_value = 12345
        mock_session = MagicMock()
        mock_dev.attach.return_value = mock_session

        mock_session.create_script.side_effect = Exception("script error")

        daemon = Daemon("/fake/path/target.exe", spawn=True)
        with pytest.raises(Exception, match="script error"):
            daemon.attach()

        mock_dev.resume.assert_called_once_with(12345)


def test_attach_exception_no_resume_if_not_spawn():
    with patch("livetools.server.frida") as mock_frida, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_frida.get_local_device.return_value = mock_dev

        mock_session = MagicMock()
        mock_frida.attach.return_value = mock_session

        mock_session.create_script.side_effect = Exception("script error")

        daemon = Daemon("12345", spawn=False)
        with pytest.raises(Exception, match="script error"):
            daemon.attach()

        mock_dev.resume.assert_not_called()


def test_attach_script_exception_spawn_mode():
    with patch("frida.get_local_device") as mock_get_local_device, \
         patch("livetools.server.AGENT_JS") as mock_agent_js:

        mock_dev = MagicMock()
        mock_get_local_device.return_value = mock_dev
        mock_dev.spawn.return_value = 300

        mock_session = MagicMock()
        mock_dev.attach.return_value = mock_session

        mock_agent_js.read_text.return_value = "dummy_js"
        mock_session.create_script.side_effect = Exception("script failed")

        daemon = Daemon("dummy_target", spawn=True)
        with pytest.raises(Exception, match="script failed"):
            daemon.attach()

        mock_dev.resume.assert_called_once_with(300)
