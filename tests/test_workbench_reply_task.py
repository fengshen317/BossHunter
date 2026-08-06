"""Regression coverage for the dashboard-confirmed reply task."""

from threading import Event

from bosshunter.web.tasks import WorkbenchTaskRunner


def test_reply_task_is_accepted_and_runs_once() -> None:
    """A confirmed dashboard reply must run through the single-task runner."""
    executed = Event()
    runner = WorkbenchTaskRunner({"reply": lambda task, config: executed.set()})

    task = runner.start("reply", {})

    assert task["mode"] == "reply"
    assert executed.wait(timeout=1)


def test_reenrich_task_is_accepted_and_runs_once() -> None:
    """企业信息补全也必须走单任务执行器，避免与发送任务并发。"""
    executed = Event()
    runner = WorkbenchTaskRunner({"reenrich": lambda task, config: executed.set()})

    task = runner.start("reenrich", {})

    assert task["mode"] == "reenrich"
    assert executed.wait(timeout=1)
