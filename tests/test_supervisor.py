from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from nora import supervisor as sup

ROOT = Path(__file__).resolve().parent.parent


def test_alive_detects_the_current_process():
    assert sup._alive(os.getpid()) is True


def test_alive_rejects_a_used_up_pid():
    assert sup._alive(999_999) is False
    assert sup._alive(0) is False


def test_command_defaults_to_the_bot(monkeypatch):
    monkeypatch.delenv("NORA_SUPERVISOR_CMD", raising=False)
    assert sup._command()[1:] == ["-m", "nora", "bot"]


def test_command_honours_the_env_override(monkeypatch):
    monkeypatch.setenv("NORA_SUPERVISOR_CMD", json.dumps(["python", "-c", "pass"]))
    assert sup._command() == ["python", "-c", "pass"]


def test_pid_file_blocks_a_second_supervisor(tmp_path):
    pid_file = tmp_path / "nora.pid"
    assert sup._acquire_pid_file(pid_file) is True
    # our own process is alive, so a second start must be refused
    assert sup._acquire_pid_file(pid_file) is False


def test_stale_pid_file_is_reclaimed(tmp_path):
    pid_file = tmp_path / "nora.pid"
    pid_file.write_text("999999", encoding="utf-8")
    assert sup._acquire_pid_file(pid_file) is True


def test_pids_named_finds_this_process():
    if sys.platform != "win32":
        return
    # the Store python alias re-execs, so trust the image name, not sys.executable
    image = Path(sup._process_image_path(os.getpid()) or "").name
    assert image
    assert os.getpid() in sup._pids_named(image)


def test_process_image_path_points_at_an_executable():
    path = sup._process_image_path(os.getpid())
    assert path is not None
    assert path.lower().endswith(".exe")
    assert Path(path).exists()


def test_model_server_pids_only_lists_live_processes():
    for pid in sup.model_server_pids():
        assert sup._alive(pid)


def test_supervisor_restarts_the_child_and_stops_cleanly(tmp_path):
    data = tmp_path / "data"
    marker = tmp_path / "runs.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, time\n"
        f"path = pathlib.Path({str(marker)!r})\n"
        "path.write_text((path.read_text() if path.exists() else '') + 'x')\n"
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "NORA_DATA_DIR": str(data),
        "NORA_SUPERVISOR_CMD": json.dumps([sys.executable, str(child)]),
        "NORA_RESTART_SECONDS": "0.4",
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "nora", "supervisor"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if marker.exists() and marker.read_text().count("x") >= 2:
                break
            time.sleep(0.2)

        # it really did bring the child back after it exited
        assert marker.exists()
        assert marker.read_text().count("x") >= 2

        pid_file = data / "nora.pid"
        assert pid_file.exists()
        # the Store python shim means this is the interpreter pid, not Popen's
        recorded = int(pid_file.read_text())
        assert recorded > 0
        assert sup._alive(recorded) is True

        # the same sentinel stop.ps1 writes
        (data / "stop").touch()
        proc.wait(timeout=40)

        assert proc.returncode == 0
        assert not pid_file.exists()
        assert not (data / "stop").exists()
        assert "stopped" in (data / "supervisor.log").read_text(encoding="utf-8")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
