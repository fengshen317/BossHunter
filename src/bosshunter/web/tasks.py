"""Workbench background task runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock, Thread, Timer
from typing import Any, Callable
from uuid import uuid4
import json

from bosshunter.db import get_db
from bosshunter.throttle import SendWindowChecker


MODE_LABELS = {
    "full": "运行全流程",
    "collect": "单独采集",
    "score": "仅评分待处理岗位",
    "reenrich": "补全企业信息",
    "greeting": "重新生成招呼语",
    "monitor": "单独监测",
    "deliver": "确认投递",
    "reply": "确认回复",
    "import": "定向采集岗位",
}

TERMINAL_STATUSES = {"completed", "failed", "stopped"}
ACTIVE_STATUSES = {"running", "stopping"}
# 流程阶段的规范取值，前端流程条按这个顺序渲染。
# 执行器在推进阶段时通过 _log(..., stage=...) 显式写入，前端不再解析日志文案。
PIPELINE_STAGE_KEYS = ("collect", "score", "confirm", "greet", "send")
DEADLINE_MODES = {"full", "monitor", "deliver"}
# 可与长期任务并行的短任务。它们只操作独立的 BOSS 会话/详情页/搜索页，
# 打开独立标签页，不影响监测/采集共享的列表页与数据库（WAL 并发安全），
# 因此不必与长任务互斥。同类型短任务之间仍互斥。
PARALLEL_SAFE_MODES = {"reply", "import"}


class TaskAlreadyRunningError(RuntimeError):
    """Raised when a mutually exclusive workbench task is already active."""


@dataclass
class WorkbenchTask:
    id: str
    mode: str
    label: str
    status: str = "running"
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    deadline_at: str | None = None
    stop_reason: str | None = None
    # 当前所处流程阶段，取值见 PIPELINE_STAGE_KEYS；None 表示尚未进入任何已知阶段。
    stage: str | None = None
    stop_requested: Event = field(default_factory=Event, repr=False)
    context: dict[str, Any] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "label": self.label,
            "status": self.status,
            "logs": list(self.logs),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
            "stop_reason": self.stop_reason,
            "stage": self.stage,
            "stop_requested": self.stop_requested.is_set(),
        }


Executor = Callable[[WorkbenchTask, dict], None]


class WorkbenchTaskRunner:
    def __init__(self, executors: dict[str, Executor] | None = None):
        self._executors = executors or {}
        self._tasks: dict[str, WorkbenchTask] = {}
        self._threads: dict[str, Thread] = {}
        self._deadline_timers: dict[str, Timer] = {}
        self._lock = Lock()
        self._restore_tasks()

    def start(self, mode: str, config: dict) -> dict:
        if mode not in MODE_LABELS:
            raise ValueError(f"Unsupported workbench mode: {mode}")

        with self._lock:
            active = self._active_task_locked()
            if active:
                # 并行安全任务（reply）可在长任务运行期间启动；其余组合仍互斥。
                can_parallel = mode in PARALLEL_SAFE_MODES and active.mode not in PARALLEL_SAFE_MODES
                if not can_parallel:
                    raise TaskAlreadyRunningError(
                        f"当前已有后台任务「{active.label}」正在运行或停止中，请等待其完全结束"
                    )

            task = WorkbenchTask(id=str(uuid4()), mode=mode, label=MODE_LABELS[mode])
            deadline = _deadline_from_config(mode, config)
            if deadline:
                task.deadline_at = deadline.isoformat(timespec="seconds")
            self._tasks[task.id] = task
            self._persist(task)

            if deadline and deadline <= datetime.now():
                task.stop_requested.set()
                task.status = "stopped"
                task.stop_reason = "今日发送时间窗口已截止，后台未启动"
                task.logs.append(task.stop_reason)
                task.updated_at = datetime.now().isoformat(timespec="seconds")
                return task.snapshot()

            thread = Thread(target=self._run, args=(task, config), daemon=True)
            self._threads[task.id] = thread
            if deadline:
                delay_seconds = max((deadline - datetime.now()).total_seconds(), 0)
                timer = Timer(delay_seconds, self._stop_at_deadline, args=(task.id,))
                timer.daemon = True
                self._deadline_timers[task.id] = timer
                timer.start()
            thread.start()
            return task.snapshot()

    def status(self) -> dict:
        with self._lock:
            active = self._active_task_locked()
            tasks = [task.snapshot() for task in self._tasks.values()]
            return {
                "active": active.snapshot() if active else None,
                "last_task": tasks[-1] if tasks else None,
                "tasks": tasks,
            }

    def stop(self, task_id: str, reason: str = "用户已请求停止") -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.status in TERMINAL_STATUSES:
                return task.snapshot()
            task.stop_requested.set()
            task.status = "stopping"
            task.stop_reason = reason
            if reason and (not task.logs or task.logs[-1] != reason):
                task.logs.append(reason)
            task.updated_at = datetime.now().isoformat(timespec="seconds")
            self._persist(task)
            confirmation_event = task.context.get("confirmation_event")
            if isinstance(confirmation_event, Event):
                confirmation_event.set()
            return task.snapshot()

    def wait(self, timeout: float | None = None) -> None:
        threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=timeout)

    def _run(self, task: WorkbenchTask, config: dict) -> None:
        try:
            executor = self._executors.get(task.mode)
            if executor:
                executor(task, config)
            with self._lock:
                if task.stop_requested.is_set():
                    task.status = "stopped"
                else:
                    task.status = "completed"
                task.updated_at = datetime.now().isoformat(timespec="seconds")
                self._persist(task)
        except Exception as exc:
            with self._lock:
                if task.stop_requested.is_set():
                    task.status = "stopped"
                    task.error = None
                else:
                    task.status = "failed"
                    task.error = str(exc)
                task.updated_at = datetime.now().isoformat(timespec="seconds")
                self._persist(task)
        finally:
            with self._lock:
                timer = self._deadline_timers.pop(task.id, None)
                # 终态任务移出活动线程/定时器表，避免服务器长跑后 _threads 无限增长；
                # 任务行本身保留在 _tasks 供历史查询。
                self._threads.pop(task.id, None)
            if timer:
                timer.cancel()

    def _stop_at_deadline(self, task_id: str) -> None:
        try:
            self.stop(task_id, "已到发送时间窗口截止时间，后台自动停止")
        except KeyError:
            return

    def _active_task_locked(self) -> WorkbenchTask | None:
        for task in self._tasks.values():
            if task.status in ACTIVE_STATUSES:
                return task
        return None

    def _persist(self, task: WorkbenchTask) -> None:
        """将任务状态写入 SQLite，确保服务重启后仍能查询历史状态。"""
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO workbench_tasks
                (id, mode, label, status, logs, error, created_at, updated_at, deadline_at, stop_reason, stage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status, logs=excluded.logs,
                error=excluded.error, updated_at=excluded.updated_at, deadline_at=excluded.deadline_at,
                stop_reason=excluded.stop_reason, stage=excluded.stage""",
                (task.id, task.mode, task.label, task.status, json.dumps(task.logs, ensure_ascii=False),
                 task.error, task.created_at, task.updated_at, task.deadline_at, task.stop_reason, task.stage),
            )
            conn.commit()
        finally:
            conn.close()

    def _restore_tasks(self) -> None:
        """恢复历史任务，并把上次进程退出时未完成的任务标记为中断。"""
        conn = get_db()
        try:
            rows = conn.execute("SELECT * FROM workbench_tasks ORDER BY created_at").fetchall()
            for row in rows:
                logs = json.loads(row["logs"] or "[]")
                status = row["status"]
                stop_reason = row["stop_reason"]
                if status in ACTIVE_STATUSES:
                    status = "stopped"
                    stop_reason = "服务重启，任务未自动恢复"
                    logs.append(stop_reason)
                    conn.execute(
                        "UPDATE workbench_tasks SET status=?, stop_reason=?, logs=?, updated_at=? WHERE id=?",
                        (status, stop_reason, json.dumps(logs, ensure_ascii=False),
                         datetime.now().isoformat(timespec="seconds"), row["id"]),
                    )
                self._tasks[row["id"]] = WorkbenchTask(
                    id=row["id"], mode=row["mode"], label=row["label"], status=status,
                    logs=logs, error=row["error"], created_at=row["created_at"],
                    updated_at=row["updated_at"], deadline_at=row["deadline_at"], stop_reason=stop_reason,
                    stage=row["stage"],
                )
            conn.commit()
        finally:
            conn.close()


def _deadline_from_config(mode: str, config: dict) -> datetime | None:
    """Resolve the automatic stop deadline for long-running/send tasks."""
    if mode not in DEADLINE_MODES:
        return None
    throttle = config.get("throttle", {}) if isinstance(config, dict) else {}
    windows = throttle.get("send_windows", [])
    if not isinstance(windows, list):
        return None
    return SendWindowChecker(windows).latest_end_datetime()
