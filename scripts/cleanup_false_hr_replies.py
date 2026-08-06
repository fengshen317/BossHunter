"""清理 check_replies bug 产生的错误监测数据。

bug 影响（已在 executor/monitor.py 修复）：
  - check_replies 拿聊天列表预览（=会话最后一条消息，通常是自己发的招呼语）
    当作 HR 回复，写入了 action='replied' AND detail LIKE 'HR回复:%' 的纯文本历史。
  - 同时把岗位 status 从 sent 错误推进到 replied。

本脚本做三件事（保守、精确、可回滚）：
  1. 删除所有 action='replied' AND detail LIKE 'HR回复:%' 的假历史（30 条）。
     正常流程写的是 JSON（detail LIKE '{%'），二者泾渭分明，不会误删。
  2. 回退 22 个被错误推进的岗位 replied→sent。
     判定：status=replied 且有 sent 历史、且无 auto_replied、且无 JSON 形态 replied。
  3. C 类（内容确为真 HR 拒绝，history id 在 FALSE_REPLY_HR_REJECTION_IDS 里）
     的岗位：若还不是 rejected 则改为 rejected，并补一条正常 rejected 历史。

用法：
  python scripts/cleanup_false_hr_replies.py --dry-run   # 只打印，不写入
  python scripts/cleanup_false_hr_replies.py             # 执行（自动备份 db）

执行前会自动把 data/bosshunter.db 备份为 data/bosshunter.db.bak-<时间戳>。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/bosshunter.db")

# C 类：内容确为真 HR 拒绝的假历史 id（经人工核对）。
# 这些历史行虽然格式是 bug 的纯文本，但内容是 HR 真的拒绝（"不太合适"/"简历与我们不匹配"）。
# 对应岗位应标为 rejected，而不是回退到 sent。
FALSE_REPLY_HR_REJECTION_IDS: set[int] = {74, 77, 115}

# bug 写入的假历史格式特征：纯文本，前缀固定。
# 正常流程（web/auto_replied）写的是 JSON：{"schema": "replied.v1", ...}
FAKE_REPLY_DETAIL_PREFIX = "HR回复:"


def classify(conn: sqlite3.Connection) -> dict:
    """只读分析，返回清理计划。"""
    conn.row_factory = sqlite3.Row

    # 1) 假历史：action=replied 且 detail 以 'HR回复:' 开头
    fake_rows = conn.execute(
        "SELECT id, job_id, detail FROM history "
        "WHERE action='replied' AND detail LIKE ?",
        (FAKE_REPLY_DETAIL_PREFIX + "%",),
    ).fetchall()

    # C 类（真拒绝）的假历史 id
    c_class_ids = FALSE_REPLY_HR_REJECTION_IDS & {r["id"] for r in fake_rows}
    # 其余假历史对应的 job_id
    pure_fake_job_ids = {r["job_id"] for r in fake_rows if r["id"] not in c_class_ids}

    # 2) 需回退到 sent 的岗位：
    #    status=replied，有 sent 历史，无 auto_replied，无 JSON 形态 replied，
    #    且不在 C 类（C 类要标 rejected 而非回退）。
    rollback_jobs = conn.execute(
        """
        SELECT j.id, j.company
        FROM jobs j
        WHERE j.status='replied'
          AND EXISTS (SELECT 1 FROM history h WHERE h.job_id=j.id AND h.action='sent')
          AND NOT EXISTS (SELECT 1 FROM history h WHERE h.job_id=j.id AND h.action='auto_replied')
          AND NOT EXISTS (SELECT 1 FROM history h WHERE h.job_id=j.id AND h.action='replied' AND h.detail LIKE '{%')
        """
    ).fetchall()
    rollback_ids = [r["id"] for r in rollback_jobs]
    # C 类岗位即使满足回退条件也要排除（改走 rejected 分支）
    c_class_job_ids = {
        r["job_id"] for r in fake_rows if r["id"] in c_class_ids
    }
    rollback_ids = [jid for jid in rollback_ids if jid not in c_class_job_ids]

    # 3) C 类岗位当前状态（用于决定是否需要改成 rejected）
    c_class_jobs = []
    for jid in c_class_job_ids:
        j = conn.execute(
            "SELECT id, company, status FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if j:
            c_class_jobs.append(dict(j))

    return {
        "fake_history_ids": [r["id"] for r in fake_rows],
        "c_class_history_ids": sorted(c_class_ids),
        "rollback_job_ids": rollback_ids,
        "c_class_jobs": c_class_jobs,
        "c_class_job_ids": sorted(c_class_job_ids),
    }


def apply(conn: sqlite3.Connection, plan: dict, dry_run: bool) -> None:
    """在单个事务里执行清理。dry_run=True 时只打印不提交。"""
    cur = conn.cursor()
    fake_ids = plan["fake_history_ids"]
    rollback_ids = plan["rollback_job_ids"]
    c_class_jobs = plan["c_class_jobs"]
    c_class_job_ids = plan["c_class_job_ids"]

    print(f"\n=== {'DRY-RUN（不写入）' if dry_run else '执行清理'} ===")
    print(f"1) 删除假 'HR回复:' 历史: {len(fake_ids)} 条 (ids: {fake_ids})")

    # C 类中当前还不是 rejected 的岗位 → 标 rejected + 补历史
    c_to_reject = [j for j in c_class_jobs if j["status"] != "rejected"]
    c_already_rejected = [j for j in c_class_jobs if j["status"] == "rejected"]
    print(f"2) C 类（真 HR 拒绝）岗位:")
    print(f"   - 改为 rejected: {len(c_to_reject)} 个 {[j['id'] for j in c_to_reject]}")
    print(f"   - 已是 rejected（仅删假历史）: {len(c_already_rejected)} 个 {[j['id'] for j in c_already_rejected]}")
    print(f"3) 回退 replied→sent: {len(rollback_ids)} 个 {rollback_ids}")

    if dry_run:
        print("\n[dry-run] 不做任何修改。去掉 --dry-run 执行。")
        return

    try:
        # 1) 删除假历史
        placeholders = ",".join("?" for _ in fake_ids)
        cur.execute(
            f"DELETE FROM history WHERE id IN ({placeholders})", fake_ids
        )
        deleted = cur.rowcount
        print(f"\n[ok] 删除假历史 {deleted} 条")

        # 2) C 类改为 rejected（只改还不是 rejected 的）
        for j in c_to_reject:
            cur.execute(
                "UPDATE jobs SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (j["id"],),
            )
            cur.execute(
                "INSERT INTO history (job_id, action, detail) VALUES (?, 'rejected', ?)",
                (j["id"], "清理修正：HR 实为拒绝（原被 bug 误判为 replied），已标记 rejected"),
            )
        print(f"[ok] C 类标 rejected: {len(c_to_reject)} 个（已是 rejected 的不动）")

        # 3) 回退 replied→sent
        if rollback_ids:
            placeholders = ",".join("?" for _ in rollback_ids)
            cur.execute(
                f"UPDATE jobs SET status='sent', updated_at=CURRENT_TIMESTAMP "
                f"WHERE id IN ({placeholders})",
                rollback_ids,
            )
            print(f"[ok] 回退 replied→sent: {cur.rowcount} 个")

        conn.commit()
        print("[ok] 已提交事务")
    except Exception as e:
        conn.rollback()
        print(f"[error] 失败，已回滚: {e}", file=sys.stderr)
        raise


def verify(conn: sqlite3.Connection) -> None:
    """清理后状态核对。"""
    conn.row_factory = sqlite3.Row
    print("\n=== 清理后状态 ===")
    for r in conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status ORDER BY c DESC"):
        print(f"  {r['status']:15s} {r['c']}")
    fake_left = conn.execute(
        "SELECT COUNT(*) FROM history WHERE action='replied' AND detail LIKE 'HR回复:%'"
    ).fetchone()[0]
    print(f"  剩余假 'HR回复:' 历史: {fake_left} （应为 0）")


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 check_replies bug 的错误监测数据")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[error] 数据库不存在: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        plan = classify(conn)
        if not args.dry_run:
            # 执行前自动备份
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            bak = db_path.with_suffix(db_path.suffix + f".bak-{ts}")
            shutil.copy2(db_path, bak)
            print(f"[ok] 已备份数据库到 {bak}")
        apply(conn, plan, dry_run=args.dry_run)
        if not args.dry_run:
            verify(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
