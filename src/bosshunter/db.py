"""BossHunter 业务数据库连接与岗位、历史状态仓储。"""

import re
import sqlite3
from pathlib import Path
from typing import Any


DB_PATH = Path("./data/bosshunter.db")


class PostgresConnection:
    """提供现有仓储所需接口的 psycopg 连接适配器。

    适配层只负责参数占位符和行对象兼容，避免业务模块各自维护连接逻辑。
    """

    def __init__(self, url: str) -> None:
        """连接 PostgreSQL 并返回同时支持键和下标访问的行。"""
        import psycopg

        def row_factory(cursor: Any) -> Any:
            if cursor.description is None:
                return lambda values: DatabaseRow()
            columns = [column.name for column in cursor.description]

            def make_row(values: tuple[Any, ...]) -> "DatabaseRow":
                return DatabaseRow(zip(columns, values))

            return make_row

        self._connection = psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://"),
            row_factory=row_factory,
            # 连接建立超时（秒）：数据库不可达时快速失败，避免每个调用方
            # 无上限地挂在建连上（尤其 assistant 持有全局锁的路径）。
            connect_timeout=10,
        )

    def execute(self, sql: str, params: Any = None) -> Any:
        """执行兼容参数化 SQL，并将 SQLite 占位符转换为 psycopg 格式。"""
        translated = sql.replace("?", "%s")
        translated = re.sub(r"(?<!:):(\w+)", r"%(\1)s", translated)
        translated = translated.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "INSERT OR IGNORE INTO" in sql and "ON CONFLICT" not in translated.upper():
            translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        translated = translated.replace(
            "(strftime('%s', 'now') - strftime('%s', updated_at))",
            "EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - updated_at))",
        )
        translated = translated.replace("date('now', %s)", "CURRENT_DATE + (%s)::interval")
        # 语义层时间过滤使用数据库端时钟表达式，此处转换为 PostgreSQL 方言。
        translated = re.sub(
            r"datetime\('now', '-(\d+) days'\)",
            r"CURRENT_TIMESTAMP - INTERVAL '\1 days'",
            translated,
        )
        translated = translated.replace("date('now')", "CURRENT_DATE")
        # psycopg 使用百分号占位符；SQL 字符串字面量中的 LIKE 通配符也必须
        # 转义为 %% 才会按普通百分号传给 PostgreSQL。
        translated = re.sub(
            r"'(?:''|[^'])*'",
            lambda match: match.group(0).replace("%", "%%"),
            translated,
        )
        return self._connection.execute(translated, params)

    def executescript(self, sql: str) -> None:
        """PostgreSQL 表结构由版本化迁移管理，运行期禁止隐式 DDL。"""
        return None

    def commit(self) -> None:
        """提交当前事务。"""
        self._connection.commit()

    def rollback(self) -> None:
        """回滚当前事务。"""
        self._connection.rollback()

    def close(self) -> None:
        """关闭底层连接。"""
        self._connection.close()

    def __enter__(self) -> "PostgresConnection":
        """进入连接上下文。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        """按执行结果提交或回滚并关闭连接。"""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


class DatabaseRow(dict[str, Any]):
    """兼容 sqlite3.Row 的 PostgreSQL 字典行。"""

    def __getitem__(self, key: str | int) -> Any:
        """支持按字段名或字段位置读取。"""
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def get_db(db_path: Path | None = None) -> Any:
    """获取业务数据库连接；生产运行强制使用 PostgreSQL。

    显式传入路径仅供现有隔离单元测试使用，不参与应用运行。
    """
    if db_path is None:
        from bosshunter.config import load_config

        database_url = str(load_config().get("database", {}).get("url") or "").strip()
        if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise RuntimeError("database.url 必须配置为 PostgreSQL，已禁用 SQLite 运行降级")
        return PostgresConnection(database_url)
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn


def get_business_db(config_path: Path) -> Any:
    """按运行配置打开唯一的 PostgreSQL 业务数据库。

    Args:
        config_path: 当前运行目录的配置文件。

    Returns:
        PostgreSQL 业务数据库连接。
    """
    from bosshunter.config import load_config

    database_url = str(load_config(config_path).get("database", {}).get("url") or "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("database.url 必须配置为 PostgreSQL")
    return PostgresConnection(database_url)


def _init_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            salary TEXT,
            city TEXT,
            experience TEXT,
            jd TEXT,
            hr_name TEXT,
            hr_title TEXT,
            hr_active TEXT,
            company_size TEXT,
            company_industry TEXT,
            district TEXT,
            company_background TEXT,
            registered_capital TEXT,
            url TEXT,
            score INTEGER DEFAULT 0,
            score_reason TEXT,
            greeting TEXT,
            conversation_id TEXT,
            status TEXT DEFAULT 'pending',
            error_retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );

		CREATE TABLE IF NOT EXISTS job_events (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			job_id TEXT NOT NULL,
			event_type TEXT NOT NULL,
			event_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			source TEXT NOT NULL DEFAULT 'application',
			from_status TEXT,
			to_status TEXT,
			attributes_json TEXT NOT NULL DEFAULT '{}',
			FOREIGN KEY (job_id) REFERENCES jobs(id)
		);

        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS workbench_tasks (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            label TEXT NOT NULL,
            status TEXT NOT NULL,
            logs TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline_at TEXT,
            stop_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
        CREATE INDEX IF NOT EXISTS idx_history_job_id ON history(job_id);
		CREATE INDEX IF NOT EXISTS idx_job_events_job_time ON job_events(job_id, event_time);
		CREATE INDEX IF NOT EXISTS idx_job_events_type_time ON job_events(event_type, event_time);
        CREATE INDEX IF NOT EXISTS idx_risk_events_type ON risk_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_workbench_tasks_updated_at ON workbench_tasks(updated_at);
    """)
    conn.executescript("""
		CREATE TRIGGER IF NOT EXISTS trg_jobs_collected_event
		AFTER INSERT ON jobs
		BEGIN
			INSERT INTO job_events (job_id, event_type, event_time, source, to_status)
			VALUES (NEW.id, 'job_collected', NEW.created_at, 'database_trigger', NEW.status);
		END;

		CREATE TRIGGER IF NOT EXISTS trg_jobs_status_event
		AFTER UPDATE OF status ON jobs
		WHEN OLD.status IS NOT NEW.status
		BEGIN
			INSERT INTO job_events (job_id, event_type, source, from_status, to_status)
			VALUES (
				NEW.id,
				CASE NEW.status
					WHEN 'filtered' THEN 'prefilter_rejected'
					WHEN 'scored' THEN 'ai_scored'
					WHEN 'ready' THEN 'greeting_generated'
					WHEN 'approved' THEN 'user_approved'
					WHEN 'rejected' THEN 'user_rejected'
					WHEN 'sent' THEN 'greeting_sent'
					WHEN 'replied' THEN 'hr_replied'
					WHEN 'needs_resume' THEN 'resume_requested'
					WHEN 'resume_sent' THEN 'resume_sent'
					WHEN 'follow_up_sent' THEN 'follow_up_sent'
					WHEN 'error' THEN 'send_failed'
					ELSE 'status_changed'
				END,
				'database_trigger',
				OLD.status,
				NEW.status
			);
		END;

		CREATE VIEW IF NOT EXISTS semantic_job_current AS
		SELECT id AS job_id, title, company, city, district, experience, company_size,
			company_industry, score AS ai_score, quick_score, status AS current_status,
			CASE WHEN title <> 'HR主动沟通' AND TRIM(COALESCE(title, '')) <> ''
				AND TRIM(COALESCE(jd, '')) <> '' THEN 1 ELSE 0 END AS is_formal_job,
			created_at AS collected_at, updated_at
		FROM jobs;

		CREATE VIEW IF NOT EXISTS semantic_job_funnel AS
		SELECT j.id AS job_id, j.created_at AS collected_at,
			MIN(CASE WHEN e.event_type = 'ai_scored' THEN e.event_time END) AS scored_at,
			MIN(CASE WHEN e.event_type = 'user_approved' THEN e.event_time END) AS approved_at,
			MIN(CASE WHEN e.event_type = 'greeting_sent' THEN e.event_time END) AS sent_at,
			MIN(CASE WHEN e.event_type = 'hr_replied' THEN e.event_time END) AS first_replied_at,
			MIN(CASE WHEN e.event_type = 'resume_requested' THEN e.event_time END) AS resume_requested_at,
			MIN(CASE WHEN e.event_type = 'resume_sent' THEN e.event_time END) AS resume_sent_at,
			MIN(CASE WHEN e.event_type = 'follow_up_sent' THEN e.event_time END) AS follow_up_at
		FROM jobs j LEFT JOIN job_events e ON e.job_id = j.id
		GROUP BY j.id, j.created_at;
    """)
    conn.commit()
    _migrate_v1_1(conn)


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    """Check if a job already exists in the database."""
    row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Return a job by ID for enrichment checks."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def insert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    """Insert a new job record."""
    job = {
        "district": "",
        "company_background": "",
        "registered_capital": "",
        **job,
    }
    conn.execute("""
        INSERT OR IGNORE INTO jobs (id, title, company, salary, city, experience, jd,
            hr_name, hr_title, hr_active, company_size, company_industry, district,
            company_background, registered_capital, url)
        VALUES (:id, :title, :company, :salary, :city, :experience, :jd,
            :hr_name, :hr_title, :hr_active, :company_size, :company_industry, :district,
            :company_background, :registered_capital, :url)
    """, job)
    conn.commit()


def update_job_enrichment(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    """Fill missing company and location metadata without overwriting existing values."""
    conn.execute("""
        UPDATE jobs
        SET district = COALESCE(NULLIF(district, ''), NULLIF(:district, '')),
            company_size = COALESCE(NULLIF(company_size, ''), NULLIF(:company_size, '')),
            company_industry = COALESCE(NULLIF(company_industry, ''), NULLIF(:company_industry, '')),
            company_background = COALESCE(NULLIF(company_background, ''), NULLIF(:company_background, '')),
            registered_capital = COALESCE(NULLIF(registered_capital, ''), NULLIF(:registered_capital, '')),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
    """, job)
    conn.commit()


def update_job_score(conn: sqlite3.Connection, job_id: str, score: int, reason: str) -> None:
    """Update job matching score."""
    conn.execute(
        "UPDATE jobs SET score = ?, score_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (score, reason, job_id)
    )
    conn.commit()


def update_job_greeting(conn: sqlite3.Connection, job_id: str, greeting: str) -> None:
    """Update job greeting message."""
    conn.execute(
        "UPDATE jobs SET greeting = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (greeting, job_id)
    )
    conn.commit()


def reset_job_greeting(conn: sqlite3.Connection, job_ids: list[str]) -> None:
    """Clear generated greetings so selected jobs can be regenerated for review."""
    if not job_ids:
        return
    placeholders = ", ".join("?" for _ in job_ids)
    conn.execute(
        f"UPDATE jobs SET greeting = '', status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
        job_ids,
    )
    conn.commit()


def update_job_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Update job status."""
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, job_id)
    )
    conn.commit()


def update_job_hr_active(conn: sqlite3.Connection, job_id: str, hr_active: str) -> None:
    """Persist the HR activity observed immediately before a send attempt."""
    if not hr_active.strip():
        return
    conn.execute(
        "UPDATE jobs SET hr_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (hr_active.strip(), job_id),
    )
    conn.commit()


def update_job_status_with_retry(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
) -> None:
    """Update job status and track send-error retry attempts.

    - On ``error``: increment ``error_retry_count`` so transient failures can be
      automatically re-queued after a backoff window (see ``recover_send_errors``).
    - On any other (success) status: reset ``error_retry_count`` to 0.

    This keeps permanent business failures (e.g. page taken down) distinguishable
    from recoverable infrastructure hiccups without weakening safety checks.
    """
    if status == "error":
        conn.execute(
            "UPDATE jobs SET status = 'error', "
            "error_retry_count = COALESCE(error_retry_count, 0) + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (job_id,),
        )
    else:
        conn.execute(
            "UPDATE jobs SET status = ?, error_retry_count = 0, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, job_id),
        )
    conn.commit()


def recover_send_errors(
    conn: sqlite3.Connection,
    recover_after_seconds: int = 600,
    max_retries: int = 3,
) -> int:
    """Re-queue jobs stuck in ``error`` after the backoff window elapses.

    Moves jobs whose ``status='error'`` and ``error_retry_count < max_retries``
    back to ``approved`` once ``recover_after_seconds`` have passed since their last
    update, so transient send failures (network hiccup, slow page load, selector
    drift) get a fresh chance without manual workbench intervention.

    Recovered jobs go straight back to ``approved`` (not ``ready``): they were
    already human-confirmed before the send attempt, and the send queue only
    accepts confirmed jobs — reverting to ``ready`` would require re-confirmation
    and would make them invisible to the sender.

    Returns the number of jobs recovered. Jobs that exhausted ``max_retries``
    stay ``error`` until explicitly retried via the workbench.
    """
    rows = conn.execute(
        "SELECT id FROM jobs "
        "WHERE status = 'error' "
        "  AND COALESCE(error_retry_count, 0) < ? "
        "  AND (strftime('%s', 'now') - strftime('%s', updated_at)) >= ?",
        (max_retries, recover_after_seconds),
    ).fetchall()
    if not rows:
        return 0
    recovered_ids = [row["id"] for row in rows]
    placeholders = ", ".join("?" for _ in recovered_ids)
    conn.execute(
        f"UPDATE jobs SET status = 'approved', updated_at = CURRENT_TIMESTAMP "
        f"WHERE id IN ({placeholders})",
        recovered_ids,
    )
    conn.commit()
    return len(recovered_ids)


def add_history(conn: sqlite3.Connection, job_id: str, action: str, detail: str = "") -> None:
    """Add a history record."""
    conn.execute(
        "INSERT INTO history (job_id, action, detail) VALUES (?, ?, ?)",
        (job_id, action, detail)
    )
    conn.commit()


def get_jobs_by_status(conn: sqlite3.Connection, status: str) -> list[dict]:
    """Get all jobs with a given status."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC", (status,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_jobs_needing_enrichment(
	conn: sqlite3.Connection,
	statuses: list[str] | None = None,
) -> list[dict]:
	"""Get jobs missing company/location metadata for re-enrichment.

	A job "needs enrichment" when any of district / company_background /
	registered_capital is blank — these are the same fields the scraper uses
	to decide whether to re-open a detail page. Optionally filter by status
	(e.g. only re-enrich ready/pending jobs, leave sent jobs alone).

	Args:
		conn: Open database connection.
		statuses: Restrict to these statuses. None = all statuses.
	"""
	query = (
		"SELECT * FROM jobs "
		"WHERE (district IS NULL OR district = '' "
		"    OR company_background IS NULL OR company_background = '' "
		"    OR registered_capital IS NULL OR registered_capital = '') "
	)
	params: list = []
	if statuses:
		placeholders = ", ".join("?" for _ in statuses)
		query += f"AND status IN ({placeholders}) "
		params.extend(statuses)
	query += "ORDER BY created_at DESC"
	rows = conn.execute(query, params).fetchall()
	return [dict(row) for row in rows]


def get_jobs_pending_confirmation(conn: sqlite3.Connection) -> list[dict]:
    """获取评分通过、待人工确认的岗位（招呼语已在评分阶段生成）。"""
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE status = 'ready'
        ORDER BY score DESC
    """).fetchall()
    return [dict(row) for row in rows]


def get_jobs_ready_to_send(conn: sqlite3.Connection, include_send_errors: bool = False) -> list[dict]:
    """Get jobs with generated greetings that are eligible to send.

    Only ``approved`` (human-confirmed) jobs are eligible. This is the
    enforcement point for the "human confirmation is mandatory before any
    send" rule: ``ready`` jobs have a generated greeting but have not been
    reviewed by the user, so the sender must never pick them up.

    Args:
        conn: Open database connection.
        include_send_errors: Include prior send failures for an explicit manual retry.
            Normal batch sends leave this disabled so failed jobs are not retried
            automatically.
    """
    statuses = ("approved", "error") if include_send_errors else ("approved",)
    placeholders = ", ".join("?" for _ in statuses)
    rows = conn.execute(f"""
        SELECT * FROM jobs
        WHERE status IN ({placeholders})
          AND greeting IS NOT NULL
          AND TRIM(greeting) != ''
        ORDER BY score DESC
    """, statuses).fetchall()
    return [dict(row) for row in rows]


def get_jobs_approved_to_send(conn: sqlite3.Connection) -> list[dict]:
    """Get user-confirmed jobs whose greeting is ready to send.

    Powers the workbench's "待发招呼语" section. Unlike
    ``get_jobs_ready_to_send`` (which the sender uses and which also covers
    ``ready`` and recovered jobs), this only returns ``approved`` jobs so the
    section no longer overlaps with "今日待确定" (``ready``). The send path is
    unaffected because it still goes through ``get_jobs_ready_to_send``.
    """
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE status = 'approved'
          AND greeting IS NOT NULL
          AND TRIM(greeting) != ''
        ORDER BY score DESC
    """).fetchall()
    return [dict(row) for row in rows]


def get_jobs_needing_greeting(conn: sqlite3.Connection) -> list[dict]:
    """Get jobs that need a greeting generated before sending.

    包含两类：(1) 已确认 approved 但招呼语为空；(2) 评分阶段已置 ready 但
    招呼语生成失败（AI 返回空/异常被静默吞掉）导致卡在 ready 且招呼语为空。
    第二类若不纳入会永久不可见（sender 过滤空招呼语），必须能被 regenerate 恢复。
    """
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE status IN ('approved', 'ready')
          AND (greeting IS NULL OR TRIM(greeting) = '')
        ORDER BY score DESC
    """).fetchall()
    return [dict(row) for row in rows]


def get_jobs_with_send_errors(conn: sqlite3.Connection) -> list[dict]:
    """Get jobs where greeting sending failed and can be retried."""
    rows = conn.execute("""
        SELECT * FROM jobs
        WHERE status = 'error'
          AND greeting IS NOT NULL
          AND TRIM(greeting) != ''
        ORDER BY updated_at DESC, score DESC
    """).fetchall()
    return [dict(row) for row in rows]


def get_pending_scored_jobs(conn: sqlite3.Connection, threshold: int = 60) -> list[dict]:
    """Get jobs that passed scoring and are pending confirmation."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'scored' AND score >= ? ORDER BY score DESC",
        (threshold,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Get job status statistics."""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    ).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


def _migrate_v1_1(conn: sqlite3.Connection) -> None:
    """Add v1.1 columns if they don't exist."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "quick_score" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN quick_score INTEGER DEFAULT 0")
    if "resume_path" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_path TEXT DEFAULT NULL")
    if "district" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN district TEXT DEFAULT NULL")
    if "company_background" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN company_background TEXT DEFAULT NULL")
    if "registered_capital" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN registered_capital TEXT DEFAULT NULL")
    if "conversation_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN conversation_id TEXT DEFAULT NULL")
    if "error_retry_count" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN error_retry_count INTEGER DEFAULT 0")
    conn.commit()


def update_job_quick_score(conn: sqlite3.Connection, job_id: str, quick_score: int) -> None:
    """Update job quick (pre-filter) score."""
    conn.execute(
        "UPDATE jobs SET quick_score = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (quick_score, job_id)
    )
    conn.commit()


def update_job_conversation_id(conn: sqlite3.Connection, job_id: str, conversation_id: str) -> None:
    """保存岗位对应的稳定聊天会话标识。"""
    conn.execute(
        "UPDATE jobs SET conversation_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (conversation_id, job_id),
    )
    conn.commit()


def add_risk_event(conn: sqlite3.Connection, event_type: str, detail: str = "") -> None:
    """Record a risk/anti-ban event."""
    conn.execute(
        "INSERT INTO risk_events (event_type, detail) VALUES (?, ?)",
        (event_type, detail)
    )
    conn.commit()


def get_funnel_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Get funnel stage counts for dashboard."""
    total = conn.execute("SELECT COUNT(*) as cnt FROM jobs").fetchone()["cnt"]
    prefilter_passed = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status != 'filtered' OR (status = 'filtered' AND score > 0)").fetchone()["cnt"]
    ai_scored = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('scored', 'ready', 'approved', 'rejected', 'sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent')").fetchone()["cnt"]
    approved = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('approved', 'sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent')").fetchone()["cnt"]
    sent = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent')").fetchone()["cnt"]
    replied = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('replied', 'resume_sent', 'needs_resume')").fetchone()["cnt"]
    resume_sent = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'resume_sent'").fetchone()["cnt"]
    needs_resume = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'needs_resume'").fetchone()["cnt"]
    resume_generated = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE resume_path IS NOT NULL AND TRIM(resume_path) != ''").fetchone()["cnt"]
    follow_up = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'follow_up_sent'").fetchone()["cnt"]
    rejected = conn.execute("SELECT COUNT(*) as cnt FROM jobs WHERE status = 'rejected'").fetchone()["cnt"]
    return {"采集总数": total, "初筛通过": prefilter_passed, "AI评分": ai_scored, "人工确认": approved, "发送": sent, "回复": replied, "简历已发": resume_sent, "待手动发简历": needs_resume, "简历生成": resume_generated, "跟进": follow_up, "拒绝": rejected}


def get_daily_activity(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Get daily activity for last N days."""
    rows = conn.execute("""
        SELECT date(created_at) as day, action, COUNT(*) as cnt
        FROM history
        WHERE created_at >= date('now', ?)
        GROUP BY date(created_at), action
        ORDER BY day DESC
    """, (f"-{days} days",)).fetchall()
    return [dict(row) for row in rows]


def get_top_companies(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Get top companies by average score."""
    rows = conn.execute("""
        SELECT company, ROUND(AVG(score), 0) as avg_score, COUNT(*) as job_count
        FROM jobs WHERE score > 0
        GROUP BY company
        ORDER BY avg_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_recent_history(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Get recent history entries with job info."""
    rows = conn.execute("""
        SELECT h.id, h.job_id, h.action, h.detail, h.created_at, j.company, j.title,
               j.resume_path,
               CASE
                 WHEN h.action = 'resume_failed'
                  AND (
                    (j.resume_path IS NOT NULL AND TRIM(j.resume_path) != '')
                    OR EXISTS (
                      SELECT 1
                      FROM history r
                      WHERE r.job_id = h.job_id
                        AND r.action IN ('needs_resume', 'resume_sent')
                        AND r.id > h.id
                    )
                  )
                 THEN 1
                 ELSE 0
               END AS resolved
        FROM history h
        JOIN jobs j ON h.job_id = j.id
        ORDER BY h.created_at DESC, h.id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_unresolved_resume_failures(conn: sqlite3.Connection) -> list[dict]:
    """Get the latest resume generation failure for jobs not resolved by a later success."""
    rows = conn.execute("""
        SELECT h.id, h.job_id, h.action, h.detail, h.created_at, j.company, j.title,
               j.resume_path, 0 AS resolved
        FROM history h
        JOIN jobs j ON h.job_id = j.id
        WHERE h.action = 'resume_failed'
          AND h.id = (
            SELECT MAX(f.id)
            FROM history f
            WHERE f.job_id = h.job_id
              AND f.action = 'resume_failed'
          )
          AND (j.resume_path IS NULL OR TRIM(j.resume_path) = '')
          AND NOT EXISTS (
            SELECT 1
            FROM history r
            WHERE r.job_id = h.job_id
              AND r.action IN ('needs_resume', 'resume_sent')
              AND r.id > h.id
          )
        ORDER BY h.created_at DESC, h.id DESC
    """).fetchall()
    return [dict(row) for row in rows]


def count_unresolved_reply_pending(conn: sqlite3.Connection) -> int:
    """Count latest reply_pending rows that have not been resolved for each job."""
    row = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM history h
        WHERE h.action = 'reply_pending'
          AND h.id = (
            SELECT MAX(p.id)
            FROM history p
            WHERE p.job_id = h.job_id
              AND p.action = 'reply_pending'
          )
          AND NOT EXISTS (
            SELECT 1
            FROM history r
            WHERE r.job_id = h.job_id
              AND r.action IN ('reply_dismissed', 'replied', 'auto_replied')
              AND r.id > h.id
          )
    """).fetchone()
    return int(row["cnt"] or 0)


def count_unresolved_monitor_items(conn: sqlite3.Connection) -> int:
    """Count unresolved reply suggestions and resume generation failures."""
    return count_unresolved_reply_pending(conn) + len(get_unresolved_resume_failures(conn))


def get_jobs_needing_resume(conn: sqlite3.Connection) -> list[dict]:
    """Get jobs waiting for manual resume send (tailored PDF generated, not yet sent)."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'needs_resume' ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]
