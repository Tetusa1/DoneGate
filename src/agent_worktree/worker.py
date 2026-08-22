"""Provider-neutral worker process lifecycle for agent-worktree.

The adapter starts only the command persisted in a validated task definition,
inside that task's registered Git worktree.  It records process evidence but
does not decide whether the task's changes are correct or complete.
"""

from __future__ import annotations

import codecs
import math
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .evidence import (
    ExecutionArtifact,
    artifact_paths,
    create_execution_artifact,
    write_execution_metadata,
)
from .git import GitRepository, GitRepositoryError
from .leases import LeaseManager
from .models import (
    ExecutionResult,
    ExecutionStatus,
    LeaseStatus,
    TaskState,
)
from .state import (
    ExecutionNotFoundError,
    StateStoreError,
    TaskNotFoundError,
    TaskStore,
    utc_now,
)


class WorkerError(RuntimeError):
    """Base error for worker lifecycle operations."""


class WorkerPreconditionError(WorkerError):
    """Raised when task, worktree, command, or lease preconditions fail."""


class WorkerCommandError(WorkerError):
    """Raised when a command cannot be safely used or started."""


class WorkerStartError(WorkerError):
    """Raised after a failed start has been persisted as a failed execution."""

    def __init__(self, execution_id: str, message: str) -> None:
        self.execution_id = execution_id
        super().__init__(f"WorkerStartFailed: execution_id={execution_id}: {message}")


class AlreadyFinishedError(WorkerError):
    """Raised when cancellation or signalling targets a terminal execution."""

    def __init__(self, execution_id: str, status: ExecutionStatus) -> None:
        self.execution_id = execution_id
        self.status = status
        super().__init__(f"ExecutionAlreadyFinished: {execution_id} ({status.value})")


class WorkerWaitTimeout(WorkerError):
    """Raised when a caller's wait limit expires before the worker does."""


class WorkerTerminationError(WorkerError):
    """Raised when a process group could not be confirmed stopped."""


OutputCallback = Callable[[str], None]


@dataclass
class WorkerHandle:
    execution_id: str
    task_id: str
    worker_id: str
    pid: int
    started_at: datetime
    worktree_path: Path
    command: tuple[str, ...]
    timeout_seconds: float
    process: subprocess.Popen[bytes] = field(repr=False)
    artifact: ExecutionArtifact = field(repr=False)
    status: ExecutionStatus = ExecutionStatus.RUNNING
    result: ExecutionResult | None = None
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    reader_threads: tuple[threading.Thread, ...] = field(default_factory=tuple, repr=False)


def redact_command_for_storage(command: Sequence[str]) -> tuple[str, ...]:
    """Redact values following common credential flags for persisted argv."""

    sensitive_flags = {
        "--token",
        "--api-key",
        "--password",
        "--secret",
        "--authorization",
        "authorization",
    }
    redacted: list[str] = []
    redact_next = False
    for item in command:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise WorkerCommandError("worker command arguments must be non-empty strings without NUL")
        lowered = item.casefold()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if lowered in sensitive_flags:
            redacted.append(item)
            redact_next = True
            continue
        if any(lowered.startswith(flag + "=") for flag in sensitive_flags):
            redacted.append(item.split("=", 1)[0] + "=<redacted>")
            continue
        redacted.append(item)
    return tuple(redacted)


class WorkerProcess:
    """Start and observe one generic non-interactive CLI worker at a time per task."""

    def __init__(
        self,
        store: TaskStore,
        *,
        repository: GitRepository | None = None,
        lease_manager: LeaseManager | None = None,
        clock=utc_now,
        termination_grace_seconds: float = 3.0,
    ) -> None:
        if not isinstance(termination_grace_seconds, (int, float)) or not math.isfinite(
            termination_grace_seconds
        ) or termination_grace_seconds < 0:
            raise WorkerPreconditionError("termination_grace_seconds must be finite and non-negative")
        self.store = store
        self.repository = repository or GitRepository(store.repo_root)
        self.lease_manager = lease_manager or LeaseManager(store, clock=clock)
        self._clock = clock
        self.termination_grace_seconds = float(termination_grace_seconds)
        self._handles: dict[str, WorkerHandle] = {}
        self._lock = threading.RLock()

    def start(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int | float | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
    ) -> WorkerHandle:
        _validate_output_callback(on_stdout, "on_stdout")
        _validate_output_callback(on_stderr, "on_stderr")
        record = self.store.get(task_id)
        selected_worker = record.worker_id if worker_id is None else _identity(worker_id, "worker_id")
        if selected_worker != record.worker_id:
            raise WorkerPreconditionError("worker_id does not match the task definition")
        if record.state not in {TaskState.ASSIGNED, TaskState.RUNNING}:
            raise WorkerPreconditionError(
                f"task state {record.state.value} cannot start a worker"
            )
        selected_command = tuple(record.definition.worker_command)
        if command is not None:
            provided = _validate_command(command)
            if provided != selected_command:
                raise WorkerPreconditionError("command must match the task definition")
        timeout = _timeout(
            record.definition.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        environment_values = _environment(environment)
        worktree = self._validate_worktree(record)
        self._validate_lease(record, selected_worker)
        with self._lock:
            if any(
                execution.task_id == task_id
                for execution in self.store.find_incomplete_executions()
            ):
                raise WorkerPreconditionError("task already has a running execution")

            execution_id = str(uuid.uuid4())
            artifact = create_execution_artifact(self.repository.root, execution_id)
            created_at = self._clock()
            stored_command = redact_command_for_storage(selected_command)
            initial = ExecutionResult(
                execution_id=execution_id,
                task_id=task_id,
                worker_id=selected_worker,
                status=ExecutionStatus.CREATED,
                command=stored_command,
                worktree_path=str(worktree),
                pid=None,
                created_at=created_at,
                started_at=None,
                finished_at=None,
                duration_seconds=None,
                exit_code=None,
                timeout_seconds=timeout,
                artifact_dir=str(artifact.directory),
                stdout_path=str(artifact.stdout_path),
                stderr_path=str(artifact.stderr_path),
            )
            self.store.create_execution(initial)
            self._write_metadata(initial)

            task_was_running = record.state is TaskState.RUNNING
            try:
                if record.state is TaskState.ASSIGNED:
                    self.store.transition(task_id, TaskState.RUNNING)
                    task_was_running = True
                process = self._popen(selected_command, worktree, environment_values)
                started_at = self._clock()
                running = self.store.start_execution(
                    execution_id, pid=process.pid, started_at=started_at
                )
            except (OSError, WorkerCommandError, StateStoreError, WorkerError) as exc:
                if "process" in locals() and process.poll() is None:
                    self._kill_process_group(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                failed = self._finish_start_failure(initial, exc)
                if task_was_running:
                    self._mark_task_failure(task_id, f"worker start failed: {exc}")
                self._write_metadata(failed, start_error=str(exc))
                raise WorkerStartError(execution_id, str(exc)) from exc

            threads = self._start_log_pumps(
                process,
                artifact,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            handle = WorkerHandle(
                execution_id=execution_id,
                task_id=task_id,
                worker_id=selected_worker,
                pid=process.pid,
                started_at=started_at,
                worktree_path=worktree,
                command=selected_command,
                timeout_seconds=timeout,
                process=process,
                artifact=artifact,
                reader_threads=threads,
            )
            self._handles[execution_id] = handle
            self._write_metadata(running)
            return handle

    def poll(self, execution: WorkerHandle | str) -> ExecutionResult | None:
        with self._lock:
            handle = self._handle(execution)
            if handle.result is not None:
                return handle.result
            return self._poll_handle(handle)

    def wait(
        self,
        execution: WorkerHandle | str,
        *,
        timeout: float | None = None,
    ) -> ExecutionResult:
        with self._lock:
            handle = self._handle(execution)
            if handle.result is not None:
                return handle.result
            caller_deadline = None if timeout is None else time.monotonic() + _wait_timeout(timeout)
            while True:
                result = self._poll_handle(handle)
                if result is not None:
                    return result
                now = time.monotonic()
                worker_remaining = handle.timeout_seconds - (now - handle.started_monotonic)
                if worker_remaining <= 0:
                    return self._timeout_handle(handle)
                if caller_deadline is not None:
                    caller_remaining = caller_deadline - now
                    if caller_remaining <= 0:
                        raise WorkerWaitTimeout(
                            f"wait timed out for running execution: {handle.execution_id}"
                        )
                    sleep_for = min(0.05, worker_remaining, caller_remaining)
                else:
                    sleep_for = min(0.05, worker_remaining)
                time.sleep(max(0.001, sleep_for))

    def terminate(self, execution: WorkerHandle | str) -> None:
        with self._lock:
            handle = self._handle(execution)
            self._ensure_running(handle)
            self._terminate_process_group(handle.process)

    def kill(self, execution: WorkerHandle | str) -> None:
        with self._lock:
            handle = self._handle(execution)
            self._ensure_running(handle)
            self._kill_process_group(handle.process)

    def cancel(self, execution: WorkerHandle | str) -> ExecutionResult:
        with self._lock:
            handle = self._handle(execution)
            self._ensure_running(handle)
            self._terminate_process_group(handle.process)
            try:
                handle.process.wait(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                self._kill_process_group(handle.process)
                try:
                    handle.process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise WorkerTerminationError(
                        f"process group did not terminate: {handle.execution_id}"
                    ) from exc
            self._join_log_pumps(handle)
            return self._finalize(handle, ExecutionStatus.CANCELLED, handle.process.returncode)

    def result(self, execution_id: str) -> ExecutionResult:
        return self.store.get_execution(execution_id)

    def find_incomplete_executions(self) -> tuple[ExecutionResult, ...]:
        return self.store.find_incomplete_executions()

    def _poll_handle(self, handle: WorkerHandle) -> ExecutionResult | None:
        returncode = handle.process.poll()
        if returncode is None:
            if time.monotonic() - handle.started_monotonic >= handle.timeout_seconds:
                return self._timeout_handle(handle)
            return None
        self._join_log_pumps(handle)
        status = ExecutionStatus.SUCCEEDED if returncode == 0 else ExecutionStatus.FAILED
        return self._finalize(handle, status, returncode)

    def _timeout_handle(self, handle: WorkerHandle) -> ExecutionResult:
        if handle.process.poll() is None:
            self._terminate_process_group(handle.process)
            try:
                handle.process.wait(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                self._kill_process_group(handle.process)
                try:
                    handle.process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise WorkerTerminationError(
                        f"timed-out process group did not terminate: {handle.execution_id}"
                    ) from exc
        self._join_log_pumps(handle)
        return self._finalize(handle, ExecutionStatus.TIMED_OUT, handle.process.returncode)

    def _finalize(
        self,
        handle: WorkerHandle,
        status: ExecutionStatus,
        returncode: int | None,
    ) -> ExecutionResult:
        if handle.result is not None:
            return handle.result
        finished_at = self._clock()
        duration = max(0.0, time.monotonic() - handle.started_monotonic)
        result = self.store.finish_execution(
            handle.execution_id,
            status=status,
            finished_at=finished_at,
            exit_code=returncode,
            duration_seconds=duration,
        )
        handle.status = status
        handle.result = result
        self._write_metadata(result)
        self._update_task_after_execution(result)
        self._handles.pop(handle.execution_id, None)
        return result

    def _finish_start_failure(
        self, initial: ExecutionResult, error: BaseException
    ) -> ExecutionResult:
        failed = self.store.finish_execution(
            initial.execution_id,
            status=ExecutionStatus.FAILED,
            finished_at=self._clock(),
            exit_code=None,
            duration_seconds=0.0,
        )
        return failed

    def _update_task_after_execution(self, result: ExecutionResult) -> None:
        if result.status is ExecutionStatus.SUCCEEDED:
            return
        task = self.store.get(result.task_id)
        if result.status in {ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}:
            if task.state is TaskState.RUNNING:
                self.store.transition(
                    result.task_id,
                    TaskState.FAILED,
                    reason=f"worker execution {result.execution_id}: {result.status.value}",
                )
        elif result.status is ExecutionStatus.CANCELLED:
            if task.state is TaskState.RUNNING:
                self.store.transition(
                    result.task_id,
                    TaskState.BLOCKED,
                    reason=f"worker execution {result.execution_id}: cancelled",
                )

    def _mark_task_failure(self, task_id: str, reason: str) -> None:
        task = self.store.get(task_id)
        if task.state is TaskState.RUNNING:
            self.store.transition(task_id, TaskState.FAILED, reason=reason)

    def _validate_worktree(self, record) -> Path:  # type: ignore[no-untyped-def]
        raw = record.worktree_path
        if not raw:
            raise WorkerPreconditionError("task has no registered worktree_path")
        worktree = Path(raw).expanduser().resolve(strict=False)
        root = self.repository.root.resolve()
        try:
            worktree.relative_to(root)
        except ValueError as exc:
            raise WorkerPreconditionError("task worktree is outside the repository") from exc
        if worktree == root or not worktree.is_dir():
            raise WorkerPreconditionError("task worktree path is not a directory worktree")
        match = None
        for info in self.repository.worktrees():
            if _same_path(info.path, worktree):
                match = info
                break
        if match is None or match.bare or match.detached:
            raise WorkerPreconditionError("task worktree is not a registered Git branch worktree")
        if not record.branch_name or match.branch != record.branch_name:
            raise WorkerPreconditionError("task worktree branch does not match runtime metadata")
        return worktree

    def _validate_lease(self, record, worker_id: str) -> None:  # type: ignore[no-untyped-def]
        if not record.definition.write_paths:
            return
        now = self._clock()
        leases = self.lease_manager.list_task(record.task_id)
        if not any(
            lease.task_id == record.task_id
            and lease.worker_id == worker_id
            and lease.status is LeaseStatus.ACTIVE
            and lease.expires_at > now
            for lease in leases
        ):
            raise WorkerPreconditionError("task has no active write-path lease")

    def _popen(
        self,
        command: tuple[str, ...],
        worktree: Path,
        environment: dict[str, str] | None,
    ) -> subprocess.Popen[bytes]:
        options: dict[str, object] = {
            "args": list(command),
            "cwd": str(worktree),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            options["start_new_session"] = True
        return subprocess.Popen(**options)  # type: ignore[arg-type]

    def _start_log_pumps(
        self,
        process: subprocess.Popen[bytes],
        artifact: ExecutionArtifact,
        *,
        on_stdout: OutputCallback | None,
        on_stderr: OutputCallback | None,
    ) -> tuple[threading.Thread, ...]:
        if process.stdout is None or process.stderr is None:
            raise WorkerError("worker pipes were not created")
        threads = (
            threading.Thread(
                target=_pump_output,
                args=(process.stdout, artifact.stdout_path, on_stdout),
                name=f"agent-worktree-{process.pid}-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_pump_output,
                args=(process.stderr, artifact.stderr_path, on_stderr),
                name=f"agent-worktree-{process.pid}-stderr",
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        return threads

    def _join_log_pumps(self, handle: WorkerHandle) -> None:
        for thread in handle.reader_threads:
            thread.join(timeout=5)

    def _handle(self, execution: WorkerHandle | str) -> WorkerHandle:
        execution_id = execution.execution_id if isinstance(execution, WorkerHandle) else execution
        handle = self._handles.get(execution_id)
        if handle is None:
            result = self.store.get_execution(execution_id)
            if result.is_terminal:
                raise AlreadyFinishedError(execution_id, result.status)
            raise WorkerPreconditionError(
                f"execution is not attached to this process after restart: {execution_id}"
            )
        return handle

    def _ensure_running(self, handle: WorkerHandle) -> None:
        if handle.result is not None:
            raise AlreadyFinishedError(handle.execution_id, handle.result.status)
        returncode = handle.process.poll()
        if returncode is not None:
            self._join_log_pumps(handle)
            status = ExecutionStatus.SUCCEEDED if returncode == 0 else ExecutionStatus.FAILED
            result = self._finalize(handle, status, returncode)
            raise AlreadyFinishedError(handle.execution_id, result.status)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError):
                process.terminate()
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                )
            except OSError:
                process.kill()
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()

    def _write_metadata(self, result: ExecutionResult, **extra: str) -> None:
        payload = result.to_dict()
        payload.update(extra)
        artifact = artifact_paths(self.repository.root, result.execution_id)
        if artifact.directory != Path(result.artifact_dir).resolve(strict=False):
            raise WorkerError("execution artifact path does not match repository boundary")
        write_execution_metadata(artifact, payload)


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, bytes)) or not command:
        raise WorkerCommandError("worker command must be a non-empty argv sequence")
    values = tuple(command)
    if any(not isinstance(item, str) or not item or "\x00" in item for item in values):
        raise WorkerCommandError("worker command arguments must be non-empty strings without NUL")
    return values


def _validate_output_callback(callback: OutputCallback | None, field: str) -> None:
    if callback is not None and not callable(callback):
        raise WorkerPreconditionError(f"{field} must be callable")


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise WorkerPreconditionError(f"{field} must be a non-empty string")
    return value


def _timeout(value: int | float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerPreconditionError("timeout_seconds must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise WorkerPreconditionError("timeout_seconds must be a finite positive number")
    return result


def _wait_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerWaitTimeout("wait timeout must be finite and non-negative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise WorkerWaitTimeout("wait timeout must be finite and non-negative")
    return result


def _environment(environment: Mapping[str, str] | None) -> dict[str, str] | None:
    if environment is None:
        return None
    values = dict(os.environ)
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "\x00" in key
            or "\x00" in value
        ):
            raise WorkerPreconditionError("environment keys and values must be strings without NUL")
        values[key] = value
    return values


def _same_path(left: Path, right: Path) -> bool:
    left_text = str(left.resolve(strict=False))
    right_text = str(right.resolve(strict=False))
    return left_text.casefold() == right_text.casefold() if os.name == "nt" else left_text == right_text


def _pump_output(
    pipe, target: Path, callback: OutputCallback | None  # type: ignore[no-untyped-def]
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    active_callback = callback
    try:
        with target.open("ab", buffering=0) as output:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                if active_callback is not None:
                    text = decoder.decode(chunk)
                    if text:
                        try:
                            active_callback(text)
                        except Exception:
                            # Terminal observers must never interrupt artifact capture.
                            active_callback = None
            if active_callback is not None:
                tail = decoder.decode(b"", final=True)
                if tail:
                    try:
                        active_callback(tail)
                    except Exception:
                        pass
    finally:
        pipe.close()
