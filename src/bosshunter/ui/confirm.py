"""Confirmation UI - Rich terminal display for job review."""

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt

from bosshunter.db import get_db, get_jobs_pending_confirmation, update_job_status, add_history
from bosshunter.ai.greeter import regenerate_greeting

console = Console()


def _show_job_detail(index: int, job: dict) -> None:
    """打印单条岗位详情（含招呼语）。"""
    console.print(f"\n[bold]#{index}[/bold] {job['company']} - {job['title']} ({job.get('salary', '面议')})")
    console.print(f"  匹配分: {job['score']} | {job.get('score_reason', '')}")
    greeting = (job.get("greeting") or "").strip()
    if greeting:
        console.print(f"  招呼语: {greeting}")
    else:
        console.print("  招呼语: [dim]（待生成，按 r 重新生成）[/dim]")


def show_confirmation(config: dict) -> bool:
    """Display jobs for confirmation. Returns True if any jobs were approved."""
    db = get_db()
    jobs = get_jobs_pending_confirmation(db)

    if not jobs:
        console.print("[yellow]没有待确认的岗位[/yellow]")
        db.close()
        return False

    # Display summary table
    console.print(f"\n[bold cyan]═══ 投递清单 ({len(jobs)} 个岗位) ═══[/bold cyan]\n")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("公司", width=14)
    table.add_column("职位", width=20)
    table.add_column("薪资", width=10)
    table.add_column("匹配分", width=6, justify="center")
    table.add_column("评分理由", width=22)
    table.add_column("招呼语预览", width=24)

    for i, job in enumerate(jobs, 1):
        reason_preview = (job.get("score_reason", "") or "")[:20]
        if len(job.get("score_reason", "") or "") > 20:
            reason_preview += "..."

        greeting = (job.get("greeting") or "").strip()
        greeting_preview = greeting[:22] + "..." if len(greeting) > 22 else greeting
        if not greeting_preview:
            greeting_preview = "[dim]（待生成）[/dim]"

        score_style = "green" if job["score"] >= 80 else "yellow" if job["score"] >= 60 else "red"

        table.add_row(
            str(i),
            (job["company"] or "")[:12],
            (job["title"] or "")[:18],
            job.get("salary", "") or "面议",
            f"[{score_style}]{job['score']}[/{score_style}]",
            reason_preview,
            greeting_preview,
        )

    console.print(table)
    console.print()

    # Confirmation options
    choice = Prompt.ask(
        "[bold]操作[/bold]",
        choices=["a", "s", "q"],
        default="s"
    )

    if choice == "q":
        console.print("[yellow]已取消[/yellow]")
        db.close()
        return False

    if choice == "a":
        # Approve all
        for job in jobs:
            update_job_status(db, job["id"], "approved")
            add_history(db, job["id"], "approved", "批量确认")
        console.print(f"[green]✓ 已确认 {len(jobs)} 个岗位[/green]")
        db.close()
        return True

    # Individual selection mode
    approved_count = 0
    for i, job in enumerate(jobs, 1):
        _show_job_detail(i, job)
        # 默认 n（不批准）：盲按回车/脚本空输入不应意外批准敏感操作，
        # 强制用户显式选择 y 才会确认投递。
        action = Prompt.ask("  ", choices=["y", "n", "e", "r", "q"], default="n")

        if action == "q":
            break
        elif action == "y":
            update_job_status(db, job["id"], "approved")
            add_history(db, job["id"], "approved", "逐个确认")
            approved_count += 1
        elif action == "r":
            # 重新生成招呼语
            console.print("  [dim]正在重新生成招呼语...[/dim]")
            new_greeting = regenerate_greeting(job["id"], config)
            if new_greeting:
                console.print(f"  [green]  新招呼语: {new_greeting[:100]}[/green]")
                # 重新显示后让用户决定
                _show_job_detail(i, job)
                action2 = Prompt.ask("  ", choices=["y", "n", "e", "r", "q"], default="n")
                if action2 == "q":
                    break
                elif action2 == "y":
                    update_job_status(db, job["id"], "approved")
                    add_history(db, job["id"], "approved", "重生成招呼语后确认")
                    approved_count += 1
                elif action2 == "e":
                    new_greeting = Prompt.ask("  新招呼语")
                    if new_greeting:
                        from bosshunter.db import update_job_greeting
                        update_job_greeting(db, job["id"], new_greeting)
                    update_job_status(db, job["id"], "approved")
                    add_history(db, job["id"], "approved", "重生成+编辑后确认")
                    approved_count += 1
                else:
                    update_job_status(db, job["id"], "skipped")
                    add_history(db, job["id"], "skipped", "用户跳过")
            else:
                console.print("  [red]招呼语重新生成失败[/red]")
        elif action == "e":
            # Edit greeting
            new_greeting = Prompt.ask("  新招呼语")
            if new_greeting:
                from bosshunter.db import update_job_greeting
                update_job_greeting(db, job["id"], new_greeting)
            update_job_status(db, job["id"], "approved")
            add_history(db, job["id"], "approved", "编辑后确认")
            approved_count += 1
        else:
            update_job_status(db, job["id"], "skipped")
            add_history(db, job["id"], "skipped", "用户跳过")

    console.print(f"\n[green]✓ 已确认 {approved_count} 个岗位[/green]")
    db.close()
    return approved_count > 0
