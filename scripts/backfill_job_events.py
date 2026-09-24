"""把 history 里的历史动作回填成 job_events 结构化事件。

``003_semantic_layer.sql`` 的触发器只记录**执行之后**发生的状态变更，
所以历史岗位在 job_events 里没有任何事件，``semantic_job_funnel`` 的
各阶段时间列全是 NULL。本脚本按 history 的动作类型补上这段历史。

写入的行统一标记 ``source='backfill'``，便于整体撤销：

    DELETE FROM job_events WHERE source = 'backfill';

幂等：重复执行会先删掉上一次的回填行再重写，不会产生重复事件。

**回填不了的部分**：``ai_scored`` 与 ``greeting_generated`` —— 评分阶段不写
history，没有可靠来源。因此「采集 → 评分 → 确认」的耗时算不出来，
但「确认 → 发送 → 回复」这条最有用的链路可以算。

用法：
    python scripts/backfill_job_events.py --dry-run
    python scripts/backfill_job_events.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bosshunter.db import get_db  # noqa: E402


# history.action -> job_events.event_type，与 003 触发器里的 CASE 映射保持一致。
ACTION_TO_EVENT: dict[str, str] = {
	"approved": "user_approved",
	"sent": "greeting_sent",
	"replied": "hr_replied",
	"needs_resume": "resume_requested",
	"resume_sent": "resume_sent",
	"rejected": "user_rejected",
	"error": "send_failed",
	"closed": "status_changed",
}

# 刻意不回填的 history 动作：
#   reply_pending / reply_dismissed —— 回复工作流的中间态，不是岗位状态转换；
#   imported —— 每岗已有 job_collected 事件，再写一条是重复。
SKIPPED_ACTIONS = {"reply_pending", "reply_dismissed", "imported"}


def backfill(conn, dry_run: bool = False) -> dict[str, int]:
	"""回填 job_events，返回各事件类型写入的行数。

	Args:
		conn: 数据库连接。
		dry_run: 为 True 时只统计不写入（仍会回滚已删除的旧回填行）。

	Returns:
		事件类型 -> 写入行数。
	"""
	# 先清掉上一次的回填行再重写，保证幂等。dry_run 走同一个事务，
	# 计数才不会被旧行灌水；调用方在 dry_run 结束时回滚即可恢复。
	existing = conn.execute("SELECT count(*) AS c FROM job_events WHERE source = 'backfill'").fetchone()["c"]
	if existing:
		conn.execute("DELETE FROM job_events WHERE source = 'backfill'")

	counts: dict[str, int] = {}

	# 1) 每个岗位的采集事件，时间取 jobs.created_at。
	#    to_status 留空：created_at 时点的状态已不可考，填入当前状态是错的。
	conn.execute(
		"""INSERT INTO job_events (job_id, event_type, event_time, source)
		SELECT id, 'job_collected', created_at, 'backfill' FROM jobs"""
	)
	counts["job_collected"] = conn.execute(
		"SELECT count(*) AS c FROM job_events WHERE source = 'backfill' AND event_type = 'job_collected'"
	).fetchone()["c"]

	# 2) history 里的动作事件。
	for action, event_type in ACTION_TO_EVENT.items():
		conn.execute(
			"""INSERT INTO job_events (job_id, event_type, event_time, source, to_status)
			SELECT job_id, ?, created_at, 'backfill', ?
			FROM history WHERE action = ?""",
			(event_type, action, action),
		)
		counts[event_type] = conn.execute(
			"SELECT count(*) AS c FROM job_events WHERE source = 'backfill' AND event_type = ?",
			(event_type,),
		).fetchone()["c"]

	return counts


def main() -> int:
	parser = argparse.ArgumentParser(description="回填 job_events 结构化事件")
	parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
	args = parser.parse_args()

	conn = get_db()
	try:
		before = conn.execute("SELECT count(*) AS c FROM job_events").fetchone()["c"]
		counts = backfill(conn, dry_run=args.dry_run)
		if args.dry_run:
			conn.rollback()
			print("[dry-run] 未写入，预计回填：")
		else:
			conn.commit()
			print("已回填：")
		for event_type, count in counts.items():
			print(f"  {event_type:20} {count}")
		print(f"  合计 {sum(counts.values())} 条")
		if args.dry_run:
			print(f"\n当前 job_events 行数（未变）：{before}")
		else:
			after = conn.execute("SELECT count(*) AS c FROM job_events").fetchone()["c"]
			print(f"\njob_events 行数：{before} -> {after}")
	finally:
		conn.close()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
