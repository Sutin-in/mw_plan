"""Wave 10A: the service supervisor restarts, detects hangs, logs and stops cleanly."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("supervise", REPO / "tools" / "ops" / "supervise.py")
assert _spec is not None and _spec.loader is not None
sv = importlib.util.module_from_spec(_spec)
sys.modules["supervise"] = sv
_spec.loader.exec_module(sv)


def _log_text(d: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted(d.glob("*.log")))


@pytest.mark.spec("S-38", "S-25.3")
def test_a_one_shot_job_is_logged_and_returns_its_exit_code(tmp_path: Path) -> None:
    job = sv.Service(
        "job", [sys.executable, "-c", "print('hello'); raise SystemExit(4)"], tmp_path, True
    )
    log = sv.Log(tmp_path / "logs", "job", 90)
    assert sv.Supervisor(job, tmp_path / "run", log).run() == 4
    text = _log_text(tmp_path / "logs")
    assert "hello" in text and "finished with code 4" in text
    assert not (tmp_path / "run" / "job.pid").exists()


@pytest.mark.spec("S-38")
def test_a_crashed_service_is_restarted_until_a_stop_is_requested(tmp_path: Path) -> None:
    crash = sv.Service("svc", [sys.executable, "-c", "print('up'); raise SystemExit(1)"], tmp_path)
    log = sv.Log(tmp_path / "logs", "svc", 90)
    run = tmp_path / "run"
    sup = sv.Supervisor(crash, run, log)

    def pause(seconds: float) -> None:
        if sup.starts >= 3:  # after the third start, ask it to stop
            (run / "svc.stop").write_text("stop", encoding="ascii")
        threading.Event().wait(min(seconds, 0.05))

    sup.sleep = pause
    assert sup.run() == 0
    text = _log_text(tmp_path / "logs")
    assert sup.starts >= 3 and text.count("exited with code 1") >= 3
    assert "restarting svc in 5 s" in text and "restarting svc in 10 s" in text


@pytest.mark.spec("S-38")
def test_a_hung_service_is_restarted_after_failed_health_checks(tmp_path: Path) -> None:
    hang = sv.Service(
        "web",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        tmp_path,
        health_url="http://127.0.0.1:9/never",
    )
    log = sv.Log(tmp_path / "logs", "web", 90)
    run = tmp_path / "run"
    checks: list[str] = []

    def unhealthy(url: str) -> bool:
        checks.append(url)
        if len(checks) >= 4:  # the second process: stop the test
            (run / "web.stop").write_text("stop", encoding="ascii")
        return False

    sup = sv.Supervisor(
        hang, run, log, check_every=0, failures_to_restart=3, grace=0, is_healthy=unhealthy
    )
    sup.sleep = lambda s: threading.Event().wait(min(s, 0.02))
    assert sup.run() == 0
    text = _log_text(tmp_path / "logs")
    assert "not answering its health check" in text and sup.starts == 2
    assert "stop requested" in text


@pytest.mark.spec("S-38")
def test_old_logs_are_pruned_and_a_second_supervisor_is_refused(tmp_path: Path) -> None:
    log = sv.Log(tmp_path, "api", 90)
    old = tmp_path / "api-20200101.log"
    old.write_text("x", encoding="utf-8")
    keep = log.path(dt.datetime.now(dt.UTC).astimezone().date())
    keep.write_text("y", encoding="utf-8")
    log.prune(dt.datetime.now(dt.UTC).astimezone().date())
    assert not old.exists() and keep.exists()
    run = tmp_path / "run"
    run.mkdir()
    (run / "api.pid").write_text("1", encoding="ascii")  # stale: never blocks a start
    job = sv.Service("api", [sys.executable, "-c", "pass"], tmp_path, True)
    assert sv.Supervisor(job, run, log).run() == 0
    held = sv._try_lock(run / "api.lock")  # another supervisor holds the lock
    try:
        assert sv.Supervisor(job, run, log).run() == 3
    finally:
        held.close()
    assert sv.Supervisor(job, run, log).run() == 0  # released with its holder


@pytest.mark.spec("S-38")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals; Windows ends the job tree")
def test_a_terminated_supervisor_stops_its_service_first(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess

    marker = tmp_path / "child.pid"
    code = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    script = tmp_path / "run_one.py"
    ops, run, logs = str(REPO / "tools" / "ops"), str(tmp_path / "run"), str(tmp_path / "logs")
    script.write_text(
        f"import sys; sys.path.insert(0, {ops!r})\n"
        "import supervise as sv\n"
        "from pathlib import Path\n"
        f"s = sv.Service('svc', [sys.executable, '-c', {code!r}], Path({str(tmp_path)!r}))\n"
        f"log = sv.Log(Path({logs!r}), 'svc', 90)\n"
        f"raise SystemExit(sv.Supervisor(s, Path({run!r}), log).run())\n",
        encoding="utf-8",
    )
    sup = subprocess.Popen([sys.executable, str(script)])
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        threading.Event().wait(0.1)
    child = int(marker.read_text())
    sup.send_signal(signal.SIGTERM)
    assert sup.wait(30) == 0
    threading.Event().wait(0.5)
    try:
        os.kill(child, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive, "the service must not outlive its supervisor"
    assert "received signal" in _log_text(tmp_path / "logs")
