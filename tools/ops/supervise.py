"""Keep a PPR service running and logged (Wave 10A, operations). Standard library only.

    python tools/ops/supervise.py api        # the Python API (python -m ppr.cli serve)
    python tools/ops/supervise.py ui         # the user interface (node frontend/src/server.js)
    python tools/ops/supervise.py nightly    # one nightly PR synchronization, then exit
    python tools/ops/supervise.py status     # ppr.cli ops-status, then exit
    python tools/ops/supervise.py backup     # ppr.cli backup --out %PPR_BACKUP_DIR%, then exit
    python tools/ops/supervise.py --stop api # ask a running supervisor to stop its service

Windows Task Scheduler starts ``api`` and ``ui`` at system start-up and ``nightly`` at the time
IT chooses (tools/ops/register_tasks.ps1). This supervisor adds what the scheduler lacks:

* restart: when the service exits on its own, it is started again after a pause (5 s,
  doubling up to 60 s; back to 5 s once it has run for 10 minutes);
* hang detection (api, ui): when the health address stops answering 200 for 3 checks in a
  row (30 s apart) the service is stopped and started again;
* logs: every line the service writes, time-stamped, in ``logs/<name>-YYYYMMDD.log``
  (``PPR_LOG_DIR``), plus the supervisor's own events; files older than ``PPR_LOG_KEEP_DAYS``
  (default 90) are removed;
* one supervisor per service (an OS lock on ``run/<name>.lock``); a clean stop through
  ``run/<name>.stop`` or a termination signal (the service is stopped first);
* settings (ports, folders) from the environment, then the repository's ``.env``.

The exit code of a one-shot job (``nightly``, ``status``) is the job's own, so the scheduler's
"last run result" shows a failure.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"


def _parse_env_file(path: Path) -> dict[str, str]:
    """The repository's ``.env`` (KEY=VALUE; same rules as ppr.config.settings)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def setting(name: str, default: str) -> str:
    """A PPR_* setting as the services read it: the environment, then ``.env``."""
    return os.environ.get(name) or _parse_env_file(REPO / ".env").get(name) or default


API_PORT = int(setting("PPR_API_PORT", "8000"))
UI_PORT = int(setting("PPR_UI_PORT", "3000"))


@dataclass(frozen=True)
class Service:
    name: str
    command: Sequence[str]
    cwd: Path
    once: bool = False
    health_url: str | None = None


def services(python: str = sys.executable) -> dict[str, Service]:
    return {
        "api": Service(
            "api",
            [python, "-m", "ppr.cli", "serve", "--port", str(API_PORT)],
            BACKEND,
            health_url=f"http://127.0.0.1:{API_PORT}/api/health",
        ),
        "ui": Service(
            "ui",
            [setting("PPR_NODE", "node"), str(REPO / "frontend" / "src" / "server.js")],
            REPO,
            health_url=f"http://127.0.0.1:{UI_PORT}/healthz",
        ),
        "nightly": Service(
            "nightly", [python, "-m", "ppr.cli", "sync-prs", "--mode", "NIGHTLY"], BACKEND, True
        ),
        "status": Service("status", [python, "-m", "ppr.cli", "ops-status"], BACKEND, True),
        "backup": Service(
            "backup",
            [
                python,
                "-m",
                "ppr.cli",
                "backup",
                "--out",
                setting("PPR_BACKUP_DIR", str(REPO / "backups")),
            ],
            BACKEND,
            True,
        ),
    }


class Log:
    def __init__(self, directory: Path, name: str, keep_days: int) -> None:
        self.dir, self.name, self.keep_days = directory, name, keep_days
        self.lock = threading.Lock()
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, day: dt.date) -> Path:
        return self.dir / f"{self.name}-{day:%Y%m%d}.log"

    def write(self, line: str) -> None:
        now = dt.datetime.now()  # noqa: DTZ005 - server local time, as the operators read it
        with self.lock:
            if getattr(self, "_pruned", None) != now.date():  # once a day, also when running
                self._pruned = now.date()
                self.prune(now.date())
            with self.path(now.date()).open("a", encoding="utf-8") as f:
                f.write(f"{now:%Y-%m-%d %H:%M:%S} {line.rstrip()}\n")

    def prune(self, today: dt.date) -> None:
        limit = today - dt.timedelta(days=self.keep_days)
        for p in self.dir.glob(f"{self.name}-*.log"):
            stamp = p.stem.rsplit("-", 1)[-1]
            with contextlib.suppress(ValueError):
                if dt.datetime.strptime(stamp, "%Y%m%d").date() < limit:  # noqa: DTZ007
                    p.unlink()


def healthy(url: str, timeout: float = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return bool(r.status == 200)
    except Exception:  # any failure means "not healthy"
        return False


class Supervisor:
    def __init__(
        self,
        service: Service,
        run_dir: Path,
        log: Log,
        *,
        check_every: float = 30,
        failures_to_restart: int = 3,
        grace: float = 60,
        is_healthy: Callable[[str], bool] = healthy,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.s, self.run_dir, self.log = service, run_dir, log
        self.check_every, self.failures_to_restart, self.grace = (
            check_every,
            failures_to_restart,
            grace,
        )
        self.is_healthy, self.sleep = is_healthy, sleep
        self.stop_file = run_dir / f"{service.name}.stop"
        self.pid_file = run_dir / f"{service.name}.pid"
        self.starts = 0

    # ---------------------------------------------------------------- one process
    def _start(self) -> subprocess.Popen[str]:
        self.starts += 1
        proc = subprocess.Popen(
            list(self.s.command),
            cwd=self.s.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            # its own process group: stopping it never reaches the supervisor, and nothing
            # it starts outlives it
            **_group(),
        )
        self.proc = proc
        self.log.write(f"[supervisor] started {self.s.name} (pid {proc.pid}, start #{self.starts})")

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.log.write(line)

        threading.Thread(target=pump, daemon=True).start()
        return proc

    def _stop(self, proc: subprocess.Popen[str], why: str) -> None:
        self.log.write(f"[supervisor] stopping {self.s.name} (pid {proc.pid}): {why}")
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(15)

    def _watch(self, proc: subprocess.Popen[str]) -> str:
        """Wait until the process exits, a stop is requested, or it stops answering."""
        started, failures, next_check = time.monotonic(), 0, time.monotonic() + self.grace
        while True:
            if proc.poll() is not None:
                return f"exited with code {proc.returncode}"
            if self.stop_file.exists():
                self._stop(proc, "stop requested")
                return "stopped"
            if self.s.health_url and time.monotonic() >= next_check:
                next_check = time.monotonic() + self.check_every
                if self.is_healthy(self.s.health_url):
                    failures = 0
                else:
                    failures += 1
                    self.log.write(
                        f"[supervisor] health check failed ({failures}/"
                        f"{self.failures_to_restart}): {self.s.health_url}"
                    )
                    if failures >= self.failures_to_restart:
                        self._stop(proc, "not answering its health check")
                        return "restarted after failed health checks"
            self.sleep(0.5)
            if time.monotonic() - started > 600:
                self.backoff = 5.0

    # ---------------------------------------------------------------- the loop
    def run(self) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # One supervisor per service: an OS lock, released by the system whatever happens
        # (power loss, kill), so a stale file or a reused process id never blocks a start.
        lock = _try_lock(self.run_dir / f"{self.s.name}.lock")
        if lock is None:
            print(f"{self.s.name}: already supervised (see {self.pid_file})")
            return 3
        self.pid_file.write_text(str(os.getpid()), encoding="ascii")
        self.stop_file.unlink(missing_ok=True)
        self.backoff = 5.0
        self.proc: subprocess.Popen[str] | None = None
        previous = _on_terminate(self._terminated)
        try:
            while True:
                proc = self._start()
                if self.s.once:
                    code = proc.wait()
                    time.sleep(0.2)  # let the log pump finish
                    self.log.write(f"[supervisor] {self.s.name} finished with code {code}")
                    return code
                outcome = self._watch(proc)
                self.log.write(f"[supervisor] {self.s.name} {outcome}")
                if outcome == "stopped" or self.stop_file.exists():
                    return 0
                self.log.write(f"[supervisor] restarting {self.s.name} in {self.backoff:g} s")
                self.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, 60.0)
                if self.stop_file.exists():
                    return 0
        finally:
            if self.proc is not None and self.proc.poll() is None:
                self._stop(self.proc, "the supervisor is stopping")
            _on_terminate(previous)
            self.pid_file.unlink(missing_ok=True)
            self.stop_file.unlink(missing_ok=True)
            lock.close()

    def _terminated(self, signum: int, _frame: object) -> None:
        """Asked to end (Task Scheduler "End", Ctrl+C, SIGTERM): stop the service first."""
        self.log.write(f"[supervisor] received signal {signum}")
        raise SystemExit(0)


def _group() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _on_terminate(handler: object) -> object:
    import signal

    previous = signal.getsignal(signal.SIGTERM)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, handler)  # type: ignore[arg-type]
        if handler is not previous and callable(handler):
            signal.signal(signal.SIGINT, handler)
    return previous


def _try_lock(path: Path):  # type: ignore[no-untyped-def]
    """An open file holding an exclusive OS lock, or None when another process holds it."""
    f = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="supervise.py")
    ap.add_argument("service", choices=sorted(services()))
    ap.add_argument("--stop", action="store_true", help="ask the running supervisor to stop")
    args = ap.parse_args(argv)
    run_dir = Path(setting("PPR_RUN_DIR", str(REPO / "run")))
    if args.stop:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{args.service}.stop").write_text("stop", encoding="ascii")
        print(f"stop requested for {args.service}")
        return 0
    log = Log(
        Path(setting("PPR_LOG_DIR", str(REPO / "logs"))),
        args.service,
        int(setting("PPR_LOG_KEEP_DAYS", "90")),
    )
    return Supervisor(services()[args.service], run_dir, log).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
