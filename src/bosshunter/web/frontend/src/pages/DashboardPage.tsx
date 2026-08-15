import { useMemo, useState } from 'react'
import { useDashboard, type HistoryItem, type Job, type WorkbenchTask } from '@/hooks/useDashboard'
import { Button } from '@/components/ui/button'
import { JobsTable } from '@/components/dashboard/JobsTable'
import { ImportJobPanel } from '@/components/dashboard/ImportJobPanel'
import { parseHistoryDetail } from '@/lib/historyDetail'
import { getActionLabel, getStatusLabel, STATUS_LABELS } from '@/lib/status'
import { formatLocalDateTime } from '@/lib/utils'
import { BrainCircuit, BriefcaseBusiness, Download, ExternalLink, Eye, MessageCircle, Play, RefreshCw, Sparkles, Square } from 'lucide-react'

type WorkbenchMode = 'full' | 'collect' | 'score' | 'monitor'
type DashboardView = 'workbench' | 'jobs' | 'monitor'

const TASK_STAGE_LABELS = [
	'开始采集岗位',
	'开始 AI 评分',
	'等待前端确认投递',
	'发送失败待处理',
	'执行一轮监测',
	'本轮监测完成，30 分钟后再次检查',
]

function currentTaskStage(logs: string[] = []) {
	for (const log of logs.slice().reverse()) {
		const stage = TASK_STAGE_LABELS.find(label => log.includes(label))
		if (stage) return stage
	}
	return '等待后端返回阶段'
}

function taskStatusText(status: string) {
	if (status === 'failed') return '运行失败'
	if (status === 'completed') return '已结束'
	if (status === 'stopped') return '已停止'
	if (status === 'stopping') return '停止中'
	return '运行中'
}

function taskStatusClass(status: string) {
	if (status === 'failed') return 'border-danger/30 bg-danger/5'
	if (status === 'completed' || status === 'stopped') return 'border-card-border bg-surface'
	return 'border-primary/30 bg-primary/5'
}

function taskStatusTitle(status: string) {
	if (status === 'completed' || status === 'stopped') return '最近任务状态'
	return '当前阶段'
}

interface DashboardPageProps {
	view?: DashboardView
}

const modes: Array<{ mode: WorkbenchMode; title: string; description: string }> = [
	{ mode: 'full', title: '运行全流程', description: '采集 → AI评分 → 确认投递 → 打招呼 → 持续监测' },
	{ mode: 'collect', title: '单独采集', description: '只采集岗位与企业信息并进入待评分队列。' },
	{ mode: 'score', title: '仅评分', description: '只对数据库中尚未完成 AI 评分的岗位重试评分。' },
	{ mode: 'monitor', title: '单独监测', description: '监测 HR 的未回复消息，并生成一条覆盖全部问题的回复建议。' },
]

const statItems = [
	{ label: '采集总数', key: '采集总数' },
	{ label: '初筛通过', key: '初筛通过', highlight: true },
	{ label: 'AI评分', key: 'AI评分' },
	{ label: '人工确认', key: '人工确认', highlight: true },
	{ label: '简历生成', key: '简历生成', highlight: true },
]

function jobSubtitle(job: Job) {
	return [job.score ? `匹配 ${job.score}` : '', job.salary, job.district || job.city, getStatusLabel(job.status)].filter(Boolean).join(' · ')
}

async function parsePreflightResponse(res: Response) {
	const rawText = await res.text()
	let data: { ok?: boolean; messages?: unknown; error?: string } = {}
	try {
		data = rawText ? JSON.parse(rawText) : {}
	} catch {
		return { ok: false, messages: [`无法解析预检响应：预检接口返回 ${res.status}`] }
	}
	const messages = Array.isArray(data.messages) ? data.messages.map(String).filter(Boolean) : []
	if (data.error) messages.push(String(data.error))
	if (!res.ok) messages.push(`预检接口返回 ${res.status}`)
	if (!data.ok && messages.length === 0) messages.push('后端未返回具体原因')
	return { ok: Boolean(res.ok && data.ok), messages }
}

export default function DashboardPage({ view = 'workbench' }: DashboardPageProps) {
	// 岗位池状态筛选上移到这里，便于透传给 useDashboard 触发服务端按页请求。
	const [jobsStatusFilter, setJobsStatusFilter] = useState('all')
	const {
		workbench,
		jobs,
		jobsTotal,
		statusCounts,
		pendingCount,
		missingCompanyInfoCount,
		history,
		loading,
		refreshing,
		error,
		refresh,
		startTask,
		stopTask,
	} = useDashboard(view, jobsStatusFilter)
	const [selected, setSelected] = useState<string[]>([])
	const [notice, setNotice] = useState('')
	const [preflightMessages, setPreflightMessages] = useState<string[]>([])
	const [selectedJob, setSelectedJob] = useState<Job | null>(null)
	const [modePending, setModePending] = useState<WorkbenchMode | null>(null)
	const [actionPending, setActionPending] = useState<string | null>(null)
	const [confirmedDeliveryIds, setConfirmedDeliveryIds] = useState<Set<string>>(new Set())

	const todayJobs = useMemo(
		() => workbench.pending_confirmation.filter(job => !confirmedDeliveryIds.has(job.id)),
		[workbench.pending_confirmation, confirmedDeliveryIds]
	)
	const pendingGreetingJobs = useMemo(
		() => workbench.pending_greetings.filter(job => !confirmedDeliveryIds.has(job.id)),
		[workbench.pending_greetings, confirmedDeliveryIds]
	)
	const needsGreetingJobs = useMemo(
		() => workbench.needs_greeting.filter(job => !confirmedDeliveryIds.has(job.id)),
		[workbench.needs_greeting, confirmedDeliveryIds]
	)
	const activeTask = workbench.task
	const visibleTask = activeTask || workbench.last_task
	const pendingReplies = history.filter(item => item.action === 'reply_pending')

	const getTaskStopReason = (task: Partial<WorkbenchTask>) => (
		task.stop_reason || task.error || '任务未启动，请检查发送时间窗口后重试。'
	)

	const toggleJob = (id: string) => {
		setSelected(prev => (prev.includes(id) ? prev.filter(item => item !== id) : [...prev, id]))
	}

	const runPreflight = async (mode: WorkbenchMode) => {
		const res = await fetch(`/api/workbench/preflight?mode=${mode}`)
		const data = await parsePreflightResponse(res)
		setPreflightMessages(data.messages)
		if (!data.ok) {
			setNotice('请按提示处理后再启动')
			return false
		}
		return true
	}

	const handleModeClick = async (mode: WorkbenchMode) => {
		if (modePending) return
		try {
			if (activeTask?.mode === mode) {
				if (window.confirm(`是否停止当前${activeTask.label}任务？已入库岗位会保留。`)) {
					setModePending(mode)
					setNotice(`正在停止${activeTask.label}...`)
					const stoppedTask = await stopTask(activeTask.id)
					setNotice(
						stoppedTask.status === 'stopped'
							? `${activeTask.label}已停止。`
							: `${activeTask.label}正在停止：会在当前浏览器操作或 AI 请求结束后退出。`
					)
				}
				return
			}
			if (activeTask) {
				setNotice(
					activeTask.status === 'stopping'
						? `当前${activeTask.label}正在停止，请等待后台完全结束后再启动其他模式。`
						: `当前正在运行${activeTask.label}，请先点击活跃卡片停止后再启动其他模式。`
				)
				return
			}
			const target = modes.find(item => item.mode === mode)
			setModePending(mode)
			setNotice(`${target?.title || '任务'}启动前预检中...`)
			if (!(await runPreflight(mode))) return
			setNotice(`${target?.title || '任务'}启动中，请稍候...`)
			const task = await startTask(mode)
			setPreflightMessages([])
			setNotice(
				task.status === 'stopped'
					? getTaskStopReason(task)
					: `${target?.title || '任务'}已启动，日志会在下方更新。`
			)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '操作失败')
		} finally {
			setModePending(null)
		}
	}

	const confirmDeliver = async (ids: string[]) => {
		if (!ids.length) return
		const count = ids.length
		if (!window.confirm(`确认投递以下 ${count} 个岗位吗？\n\n系统将使用 AI 生成的招呼语发送给对应的 HR。\n建议先在卡片中预览招呼语内容，不满意可点击「重新生成」。`)) return
		try {
			setActionPending('confirm')
			setNotice(`正在确认 ${count} 个岗位，请稍候…`)
			const res = await fetch('/api/workbench/deliver', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ job_ids: ids }),
			})
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '投递失败')
			}
			if (!ids.some(id => workbench.send_errors.some(job => job.id === id))) {
				setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
			}
			await refresh()
			setNotice(`已确认投递 ${count} 个岗位，后端会按队列推进。`)
			setSelected(prev => prev.filter(id => !new Set(ids).has(id)))
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '投递失败')
		} finally {
			setActionPending(null)
		}
	}

	const rejectSelectedJobs = async (ids: string[]) => {
		if (!ids.length) return
		const count = ids.length
		if (!window.confirm(`确定放弃这 ${count} 个岗位吗？放弃后不会进入投递，可在岗位池中查看已拒绝状态。`)) return
		try {
			setActionPending('reject')
			setNotice(`正在放弃 ${count} 个岗位，请稍候…`)
			const res = await fetch('/api/workbench/reject', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ job_ids: ids }),
			})
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '放弃失败')
			}
			const rejectedIds = new Set(ids)
			setSelected(prev => prev.filter(id => !rejectedIds.has(id)))
			setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
			await refresh()
			setNotice(`已放弃 ${count} 个岗位。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '放弃失败')
		} finally {
			setActionPending(null)
		}
	}

	const sendReadyGreetings = async (ids: string[]) => {
		if (!ids.length) return
		const count = ids.length
		try {
			setActionPending('send')
			setNotice(`正在提交 ${count} 个岗位的发送任务…`)
			const res = await fetch('/api/workbench/deliver', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ job_ids: ids, direct_send: true }),
			})
			const task = await res.json().catch(() => ({})) as Partial<WorkbenchTask>
			if (!res.ok) {
				const data = task as { error?: string }
				throw new Error(data.error || '发送失败')
			}
			await refresh()
			setNotice(
				task.status === 'stopped'
					? getTaskStopReason(task)
					: `已提交 ${count} 个岗位的发送任务，执行状态会在下方更新。`
			)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '发送失败')
		} finally {
			setActionPending(null)
		}
	}

	const regenerateGreetings = async (ids: string[]) => {
		if (!ids.length) return
		if (!window.confirm(`重新生成 ${ids.length} 条招呼语吗？这不会自动发送到 BOSS。`)) return
		try {
			setActionPending('regenerate')
			setNotice(`正在提交 ${ids.length} 条招呼语的生成任务…`)
			const res = await fetch('/api/workbench/greetings/regenerate', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ job_ids: ids }),
			})
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '重新生成失败')
			}
			await refresh()
			setNotice(`正在按新版规则重新生成 ${ids.length} 条招呼语，完成后请先查看再发送。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '重新生成失败')
		} finally {
			setActionPending(null)
		}
	}

	const regenerateGreeting = async (jobId: string) => {
		if (!window.confirm('重新生成这条招呼语吗？这不会自动发送到 BOSS。')) return
		await regenerateGreetings([jobId])
	}

	const openJobDetail = async (job: Job) => {
		try {
			const res = await fetch(`/api/jobs/${job.id}`)
			if (!res.ok) throw new Error('读取岗位详情失败')
			setSelectedJob(await res.json())
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '读取岗位详情失败')
		}
	}

	const downloadResume = (job: Job) => {
		window.open(`/api/jobs/${job.id}/resume/download`, '_blank')
	}

	const markResumeSent = async (job: Job) => {
		try {
			const res = await fetch(`/api/jobs/${job.id}/mark-resume-sent`, { method: 'POST' })
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '标记失败')
			}
			await refresh()
			setNotice(`已标记 ${job.company}｜${job.title} 的定制简历已发送。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '标记失败')
		}
	}

	if (loading) {
		return (
			<div className="flex h-full items-center justify-center">
				<RefreshCw className="h-5 w-5 animate-spin text-text-tertiary" />
			</div>
		)
	}

	if (view === 'jobs') {
		return (
			<JobsPoolView
				jobs={jobs}
				jobsTotal={jobsTotal}
				statusCounts={statusCounts}
				pendingCount={pendingCount}
				missingCompanyInfoCount={missingCompanyInfoCount}
				statusFilter={jobsStatusFilter}
				setStatusFilter={setJobsStatusFilter}
				activeTask={workbench.task}
				refresh={refresh}
				refreshing={refreshing}
				startTask={startTask}
			/>
		)
	}

	if (view === 'monitor') {
		return <MonitorExecutionView history={history} refresh={refresh} />
	}

	return (
		<div className="space-y-5 animate-fade-in">
			{/* WORKBENCH HERO */}
			<section className="rounded-lg border border-card-border bg-surface p-6 relative overflow-hidden">
				<div className="flex items-start justify-between gap-4 mb-5">
					<div>
						<p className="text-xs text-text-tertiary mb-1">Today's Workbench</p>
						<h2 className="text-xl font-bold tracking-tight text-foreground">今日求职行动</h2>
					</div>
					<div className="flex items-center gap-2">
						<Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}>
							<RefreshCw className={`mr-2 h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
							{refreshing ? '刷新中…' : '刷新'}
						</Button>
						<span className={`rounded-lg border px-3 py-1.5 text-xs ${
							activeTask
								? 'border-primary/30 bg-primary/10 text-primary'
								: 'border-card-border bg-surface-raised text-text-tertiary'
						}`}>
							{activeTask ? `${activeTask.label}中` : '空闲'}
						</span>
					</div>
				</div>

				{/* Mode cards */}
				<div className="grid grid-cols-1 gap-3 lg:grid-cols-4">
					{modes.map(item => {
						const isActive = activeTask?.mode === item.mode
						const disabled = Boolean(activeTask && !isActive)
						return (
							<button
								key={item.mode}
								onClick={() => handleModeClick(item.mode)}
								aria-disabled={disabled}
								className={`min-h-[110px] rounded-lg border p-5 text-left transition-colors duration-150 ${
									isActive
										? 'border-primary bg-primary/[0.06] text-foreground'
										: disabled
											? 'cursor-not-allowed border-card-border bg-surface text-text-tertiary opacity-50'
											: 'border-card-border bg-surface text-foreground hover:border-card-border-strong hover:shadow-card-hover'
								}`}
							>
								<div className="flex items-center justify-between gap-3 mb-2">
									<div className="text-sm font-semibold">
										{modePending === item.mode
											? activeTask?.status === 'stopping' ? '正在停止...' : '任务启动中...'
											: isActive ? activeTask?.status === 'stopping' ? `${item.title}停止中` : `${item.title}运行中` : item.title}
									</div>
									{isActive
										? <span className="flex items-center gap-1.5 text-xs font-medium text-primary"><span className="h-1.5 w-1.5 rounded-full bg-primary status-pulse" /><Square className="h-3.5 w-3.5" /></span>
										: <Play className="h-4 w-4 text-text-tertiary" />}
								</div>
								<p className={`text-xs leading-5 ${isActive ? 'text-text-secondary' : 'text-text-tertiary'}`}>{item.description}</p>
							</button>
						)
					})}
				</div>

				{/* Notices */}
				{notice && (
					<div className="mt-4 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-primary">{notice}</div>
				)}
				{preflightMessages.length > 0 && (
					<div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 text-xs text-danger">
						<div className="font-semibold text-sm">启动前需要处理：</div>
						<ul className="mt-2 list-none space-y-1">
							{preflightMessages.map((message, index) => (
								<li key={`${message}-${index}`} className="flex gap-2">
									<span className="mt-[0.45rem] h-1 w-1 shrink-0 rounded-full bg-danger" />
									<span>{message}</span>
								</li>
							))}
						</ul>
					</div>
				)}
				{error && <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 text-xs text-danger">{error}</div>}

				{/* Task status */}
				{visibleTask && (
					<div className="mt-4 rounded-lg border border-card-border bg-surface-raised p-4">
						<div className="flex flex-wrap items-start justify-between gap-3">
							<div>
								<div className="text-xs text-text-tertiary">任务运行状态</div>
								<p className="mt-1 text-[11px] leading-5 text-text-tertiary">如果点击后浏览器没有反应，请先打开 BOSS 直聘并确认已登录。</p>
							</div>
							<span className="rounded-lg border border-card-border bg-surface px-2.5 py-1 text-xs text-muted">
								{visibleTask.label}
							</span>
						</div>
						<div className={`mt-3 rounded-lg border px-4 py-3 ${taskStatusClass(visibleTask.status)}`}>
							<div className="text-xs text-text-tertiary">{taskStatusTitle(visibleTask.status)}</div>
							<div className="mt-1 text-base font-bold text-foreground">{currentTaskStage(visibleTask.logs)}</div>
							<div className="mt-1 text-xs text-muted">状态：{taskStatusText(visibleTask.status)}</div>
							{visibleTask.deadline_at && (
								<div className="mt-1 text-xs text-muted">
									截止：{new Date(visibleTask.deadline_at).toLocaleString('zh-CN', { hour12: false })}
								</div>
							)}
						</div>
						{visibleTask.error && <div className="mt-3 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{visibleTask.error}</div>}
						{visibleTask.stop_reason && <div className="mt-3 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 text-xs text-primary">{visibleTask.stop_reason}</div>}
					</div>
				)}
			</section>

			{/* FUNNEL STATS */}
			<section className="grid grid-cols-2 gap-3 lg:grid-cols-5">
				{statItems.map(item => (
					<div key={item.label} className="rounded-lg border border-card-border bg-surface p-4">
						<div className="text-xs text-text-tertiary">{item.label}</div>
						<div className={`mt-1 text-2xl font-bold tabular-nums data-value ${item.highlight ? 'text-primary' : 'text-foreground'}`}>
							{workbench.funnel[item.key] || 0}
						</div>
					</div>
				))}
			</section>

			{/* RESUME REQUESTS */}
			<section className="rounded-lg border border-card-border bg-surface p-6">
				<div className="mb-4 flex items-center justify-between gap-4">
					<div>
						<div className="flex items-center gap-2 text-xs text-warning">
							<span className="inline-block w-1.5 h-1.5 rounded-full bg-warning" />
							优先处理
						</div>
						<h3 className="mt-1 text-base font-semibold text-foreground">HR 要简历 / 定制简历下载</h3>
						<p className="mt-1 text-xs text-text-tertiary">首页只展示需要你手动下载并自行发给 HR 的定制简历事项。</p>
					</div>
					<Button variant="secondary" size="sm">查看全部简历事项</Button>
				</div>
				{workbench.needs_resume.length ? (
					<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
						{workbench.needs_resume.slice(0, 4).map(job => (
							<div key={job.id} className="rounded-lg border border-warning/20 bg-warning/5 p-4">
								<div className="flex items-start justify-between gap-3">
									<div className="font-semibold text-foreground text-sm">{job.company}｜{job.title}</div>
									<span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[11px] font-medium text-warning">简历</span>
								</div>
								<p className="mt-2 text-xs leading-5 text-text-tertiary">HR 已请求简历，系统已准备定制化简历下载入口。</p>
								<div className="mt-3 flex gap-2">
									<Button size="sm" onClick={() => downloadResume(job)}><Download className="mr-2 h-3.5 w-3.5" />下载定制简历</Button>
									<Button variant="secondary" size="sm" onClick={() => markResumeSent(job)}>标记已发送</Button>
								</div>
							</div>
						))}
					</div>
				) : (
					<div className="rounded-lg border border-dashed border-card-border bg-surface-raised p-6 text-center text-xs text-text-tertiary">暂无简历请求</div>
				)}
			</section>

			{/* SEND ERRORS */}
			{workbench.send_errors.length > 0 && (
				<section className="rounded-lg border border-danger/30 bg-danger/5 p-6">
					<div className="mb-4 flex flex-wrap items-center justify-between gap-4">
						<div>
							<h3 className="text-base font-semibold text-danger">发送失败待处理</h3>
							<p className="mt-1 text-xs text-text-tertiary">这些岗位已生成招呼语，但没有成功发送。</p>
						</div>
						<div className="flex flex-wrap gap-2">
							<Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings(workbench.send_errors.map(job => job.id))}>
								{actionPending === 'send' ? '正在提交…' : `重发全部 ${workbench.send_errors.length} 个`}
							</Button>
							<Button variant="secondary" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedJobs(workbench.send_errors.map(job => job.id))}>放弃全部</Button>
						</div>
					</div>
					<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
						{workbench.send_errors.map(job => (
							<div key={job.id} className="rounded-lg border border-danger/20 bg-surface p-4">
								<div className="flex items-start justify-between gap-3">
									<div>
										<div className="font-semibold text-foreground text-sm">{job.company}｜{job.title}</div>
										<div className="mt-1 text-xs text-danger">错误：{job.last_error || '发送失败'}</div>
									</div>
									<span className="rounded-full border border-danger/30 bg-danger/10 px-2 py-0.5 text-[11px] font-medium text-danger">失败</span>
								</div>
								<div className="mt-3 greeting-block line-clamp-2">{job.greeting || '招呼语已生成，等待重新发送。'}</div>
								<div className="mt-3 flex gap-2">
									<Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings([job.id])}>{actionPending === 'send' ? '...' : '重发'}</Button>
									<Button variant="secondary" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedJobs([job.id])}>放弃</Button>
									<Button variant="ghost" size="sm" onClick={() => openJobDetail(job)}><Eye className="mr-1.5 h-3.5 w-3.5" />详情</Button>
									<Button variant="ghost" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-1.5 h-3.5 w-3.5" />链接</Button>
								</div>
							</div>
						))}
					</div>
				</section>
			)}

			{/* PENDING GREETINGS */}
			{pendingGreetingJobs.length > 0 && (
				<section className="rounded-lg border border-primary/20 bg-primary/5 p-6">
					<div className="mb-4 flex flex-wrap items-center justify-between gap-4">
						<div>
							<div className="flex items-center gap-2 text-xs text-primary">
								<span className="inline-block w-1.5 h-1.5 rounded-full bg-primary" />
								准备发送
							</div>
							<h3 className="mt-1 text-base font-semibold text-foreground">待发送招呼语</h3>
							<p className="mt-1 text-xs text-text-tertiary">已确认并生成招呼语，点击后直接进入发送流程。</p>
						</div>
						<div className="flex flex-wrap gap-2">
							<Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings(pendingGreetingJobs.map(job => job.id))}>{actionPending === 'send' ? '...' : `发送全部 ${pendingGreetingJobs.length}`}</Button>
							<Button variant="secondary" size="sm" disabled={actionPending !== null} onClick={() => regenerateGreetings(pendingGreetingJobs.map(job => job.id))}>全部重生</Button>
							<Button variant="secondary" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedJobs(pendingGreetingJobs.map(job => job.id))}>全部放弃</Button>
						</div>
					</div>
					<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
						{pendingGreetingJobs.map(job => (
							<div key={job.id} className="rounded-lg border border-primary/20 bg-surface p-4">
								<div className="flex items-start justify-between gap-3">
									<div>
										<div className="font-semibold text-foreground text-sm">{job.company}｜{job.title}</div>
									</div>
									<span className="rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">发送</span>
								</div>
								<div className="mt-3 greeting-block line-clamp-2">{job.greeting || '招呼语已生成，等待发送。'}</div>
								<div className="mt-3 flex gap-2">
									<Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings([job.id])}>{actionPending === 'send' ? '...' : '发送'}</Button>
									<Button variant="ghost" size="sm" disabled={actionPending !== null} onClick={() => regenerateGreetings([job.id])}>重生</Button>
									<Button variant="ghost" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedJobs([job.id])}>放弃</Button>
									<Button variant="ghost" size="sm" onClick={() => openJobDetail(job)}><Eye className="mr-1.5 h-3.5 w-3.5" />详情</Button>
									<Button variant="ghost" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-1.5 h-3.5 w-3.5" />链接</Button>
								</div>
							</div>
						))}
					</div>
				</section>
			)}

			{/* NEEDS GREETING */}
			{needsGreetingJobs.length > 0 && (
				<section className="rounded-lg border border-info/20 bg-info/5 p-6">
					<div className="mb-4 flex flex-wrap items-center justify-between gap-4">
						<div>
							<div className="flex items-center gap-2 text-xs text-info">
								<span className="inline-block w-1.5 h-1.5 rounded-full bg-info" />
								待处理
							</div>
							<h3 className="mt-1 text-base font-semibold text-foreground">待生成招呼语</h3>
							<p className="mt-1 text-xs text-text-tertiary">已确认但未能生成招呼语，重新生成后可手动发送。</p>
						</div>
						<Button size="sm" onClick={() => regenerateGreetings(needsGreetingJobs.map(job => job.id))}>
							重新生成全部 {needsGreetingJobs.length} 个
						</Button>
					</div>
					<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
						{needsGreetingJobs.map(job => (
							<div key={job.id} className="rounded-lg border border-info/20 bg-surface p-4">
								<div className="font-semibold text-foreground text-sm">{job.company}｜{job.title}</div>
								<p className="mt-2 text-xs text-text-tertiary">尚未生成招呼语</p>
								<Button className="mt-3" size="sm" onClick={() => regenerateGreetings([job.id])}>重新生成</Button>
							</div>
						))}
					</div>
				</section>
			)}

			{/* TODAY PENDING CONFIRMATION */}
			<section className="rounded-lg border border-card-border bg-surface p-6">
				<div className="mb-4 flex flex-wrap items-center justify-between gap-4">
					<div>
						<h3 className="text-base font-semibold text-foreground">今日待确认</h3>
						<p className="mt-1 text-xs text-text-tertiary">需要人工确认是否推进投递的岗位，支持全选、部分选择、一键投递。</p>
					</div>
					<div className="flex gap-2">
						<Button variant="ghost" size="sm" onClick={() => setSelected(todayJobs.map(job => job.id))}>全选</Button>
						<Button variant="ghost" size="sm" onClick={() => setSelected([])}>清空</Button>
						<Button variant="secondary" size="sm" onClick={() => rejectSelectedJobs(selected)}>放弃已选 {selected.length}</Button>
						<Button size="sm" onClick={() => confirmDeliver(selected)}>投递已选 {selected.length}</Button>
					</div>
				</div>
				{todayJobs.length ? (
					<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
						{todayJobs.map(job => (
							<JobActionCard
								key={job.id}
								job={job}
								selected={selected.includes(job.id)}
								onToggle={() => toggleJob(job.id)}
								onDetail={() => openJobDetail(job)}
								onRegenerate={regenerateGreeting}
							/>
						))}
					</div>
				) : (
					<div className="rounded-lg border border-dashed border-card-border bg-surface-raised p-6 text-center text-xs text-text-tertiary">今日暂无待确认岗位</div>
				)}
			</section>
			{selectedJob && <JobDetailModal job={selectedJob} onClose={() => setSelectedJob(null)} />}
		</div>
	)
}

function JobActionCard({ job, selected, onToggle, onDetail, onRegenerate }: { job: Job; selected: boolean; onToggle: () => void; onDetail: () => void; onRegenerate: (id: string) => void }) {
	const greeting = (job.greeting || '').trim()
	return (
		<div className={`rounded-lg border p-4 transition-colors ${selected ? 'border-primary bg-primary/[0.06] shadow-card-hover' : 'border-card-border bg-surface-raised'}`}>
			<div className="flex items-start justify-between gap-3">
				<div>
					<div className="font-semibold text-foreground text-sm">{job.company}｜{job.title}</div>
					<div className="mt-1 text-xs text-text-tertiary">{jobSubtitle(job)}</div>
				</div>
				<input type="checkbox" checked={selected} onChange={onToggle} className="mt-1 h-4 w-4 accent-primary rounded" />
			</div>
			{greeting ? (
				<div className="mt-3 greeting-block line-clamp-2">{greeting}</div>
			) : (
				<p className="mt-3 text-xs leading-5 text-text-tertiary">{job.score_reason || '等待继续推进。'}</p>
			)}
			<div className="mt-3 flex flex-wrap gap-2">
				<Button variant="ghost" size="sm" onClick={onDetail}><Eye className="mr-1.5 h-3.5 w-3.5" />详情</Button>
				<Button variant="ghost" size="sm" onClick={() => onRegenerate(job.id)}><RefreshCw className="mr-1.5 h-3.5 w-3.5" />重生</Button>
				<Button variant="ghost" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-1.5 h-3.5 w-3.5" />链接</Button>
			</div>
		</div>
	)
}

function JobDetailModal({ job, onClose }: { job: Job; onClose: () => void }) {
	return (
		<div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-6 animate-fade-in">
			<div className="max-h-[86vh] w-full max-w-3xl overflow-y-auto rounded-lg border border-card-border bg-surface p-6 shadow-overlay">
				<div className="mb-4 flex items-start justify-between gap-4">
					<div>
						<h3 className="text-xl font-bold text-foreground">{job.company}｜{job.title}</h3>
						<p className="mt-1 text-xs text-text-tertiary">{job.salary || '—'} · {[job.city, job.district].filter(Boolean).join('·') || '—'} · {getStatusLabel(job.status)}</p>
					</div>
					<Button variant="secondary" size="sm" onClick={onClose}>关闭</Button>
				</div>
				<div className="grid gap-3 text-sm lg:grid-cols-2">
					<InfoBlock label="HR" value={[job.hr_name, job.hr_title].filter(Boolean).join(' · ') || '—'} />
					<InfoBlock label="公司" value={[job.company_size, job.company_background || job.company_industry, job.registered_capital && `注册资本 ${job.registered_capital}`].filter(Boolean).join(' · ') || '—'} />
					<InfoBlock label="匹配分" value={String(job.score || '—')} />
					<InfoBlock label="定制简历" value={job.resume_path || '未生成'} />
				</div>
				<div className="mt-4 rounded-lg border border-card-border bg-surface-raised p-4">
					<div className="text-xs text-text-tertiary mb-2">评分原因</div>
					<p className="whitespace-pre-wrap text-sm leading-6 text-text-secondary">{job.score_reason || '—'}</p>
				</div>
				<div className="mt-4 rounded-lg border border-card-border bg-surface-raised p-4">
					<div className="text-xs text-text-tertiary mb-2">招呼语</div>
					<div className="whitespace-pre-wrap text-sm leading-6 text-text-secondary greeting-block">{job.greeting || '未生成'}</div>
				</div>
				<div className="mt-4 rounded-lg border border-card-border bg-surface-raised p-4">
					<div className="text-xs text-text-tertiary mb-2">职位描述</div>
					<p className="whitespace-pre-wrap text-sm leading-6 text-text-secondary">{job.jd || '—'}</p>
				</div>
			</div>
		</div>
	)
}

function InfoBlock({ label, value }: { label: string; value: string }) {
	return (
		<div className="rounded-lg border border-card-border bg-surface-raised p-4">
			<div className="text-xs text-text-tertiary">{label}</div>
			<div className="mt-1 font-semibold text-foreground text-sm">{value}</div>
		</div>
	)
}

function JobsPoolView({
	jobs,
	jobsTotal,
	statusCounts,
	pendingCount,
	missingCompanyInfoCount,
	statusFilter,
	setStatusFilter,
	activeTask,
	refresh,
	refreshing,
	startTask,
}: {
	jobs: Job[]
	jobsTotal: number
	statusCounts: Record<string, number>
	pendingCount: number
	missingCompanyInfoCount: number
	statusFilter: string
	setStatusFilter: (status: string) => void
	activeTask: WorkbenchTask | null
	refresh: () => Promise<void>
	refreshing: boolean
	startTask: (mode: 'score' | 'reenrich') => Promise<WorkbenchTask>
}) {
	const [notice, setNotice] = useState('')
	const [starting, setStarting] = useState(false)
	const [regeneratingJobId, setRegeneratingJobId] = useState<string | null>(null)
	const [selectedGreetingIds, setSelectedGreetingIds] = useState<Set<string>>(new Set())
	const scoring = activeTask?.mode === 'score'
	const enriching = activeTask?.mode === 'reenrich'
	// statusCounts 现在由后端返回（全量按状态计数），筛选 pill 直接派生。
	const statusOptions = useMemo(() => {
		const knownStatuses = Object.keys(STATUS_LABELS).filter(status => statusCounts[status])
		const unknownStatuses = Object.keys(statusCounts).filter(status => !STATUS_LABELS[status])
		return [...knownStatuses, ...unknownStatuses]
	}, [statusCounts])

	const startBatchScoring = async () => {
		if (starting || activeTask) return
		setStarting(true)
		setNotice('正在检查 AI 配置并启动批量评分...')
		try {
			const preflightRes = await fetch('/api/workbench/preflight?mode=score')
			const preflight = await parsePreflightResponse(preflightRes)
			if (!preflight.ok) {
				setNotice(`无法启动：${preflight.messages.join('；')}`)
				return
			}
			await startTask('score')
			setNotice(`批量 AI 评分已启动，共 ${pendingCount} 条待处理岗位。页面会自动刷新评分结果。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '批量 AI 评分启动失败')
		} finally {
			setStarting(false)
		}
	}

	const startCompanyEnrichment = async () => {
		if (starting || activeTask || missingCompanyInfoCount === 0) return
		setStarting(true)
		setNotice('正在启动企业信息补全...')
		try {
			await startTask('reenrich')
			setNotice(`企业信息补全已启动，共 ${missingCompanyInfoCount} 个岗位待处理。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '企业信息补全启动失败')
		} finally {
			setStarting(false)
		}
	}

	const submitGreetingRegeneration = async (ids: string[]) => {
		const res = await fetch('/api/workbench/greetings/regenerate', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ job_ids: ids }),
		})
		if (!res.ok) {
			const data = await res.json().catch(() => ({}))
			throw new Error(data.error || '重新生成失败')
		}
	}

	const regeneratePoolGreeting = async (job: Job) => {
		if (activeTask || regeneratingJobId) return
		if (!window.confirm(`重新生成「${job.company} - ${job.title}」的招呼语吗？即使之前已联系过，本次仍会先进入待确认，不会自动发送。`)) return
		setRegeneratingJobId(job.id)
		setNotice('正在启动招呼语重新生成任务…')
		try {
			await submitGreetingRegeneration([job.id])
			await refresh()
			setNotice('招呼语正在重新生成，完成后岗位会回到待确认，由你确认后再发送。')
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '重新生成失败')
		} finally {
			setRegeneratingJobId(null)
		}
	}

	const regenerateSelectedGreetings = async () => {
		const ids = [...selectedGreetingIds]
		if (!ids.length || activeTask || regeneratingJobId) return
		if (!window.confirm(`批量重新生成 ${ids.length} 个岗位的招呼语吗？完成后将统一进入工作台待确认。`)) return
		setRegeneratingJobId('batch')
		setNotice(`正在启动 ${ids.length} 条招呼语的批量生成任务…`)
		try {
			await submitGreetingRegeneration(ids)
			setSelectedGreetingIds(new Set())
			await refresh()
			setNotice(`已提交 ${ids.length} 条招呼语，生成完成后会在工作台“待确认”中显示。`)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '批量重新生成失败')
		} finally {
			setRegeneratingJobId(null)
		}
	}

	const toggleGreetingJob = (jobId: string) => {
		setSelectedGreetingIds(current => {
			const next = new Set(current)
			next.has(jobId) ? next.delete(jobId) : next.add(jobId)
			return next
		})
	}

	const toggleAllGreetingJobs = (jobIds: string[]) => {
		setSelectedGreetingIds(current => {
			const next = new Set(current)
			const allSelected = jobIds.every(id => next.has(id))
			jobIds.forEach(id => allSelected ? next.delete(id) : next.add(id))
			return next
		})
	}

	return (
		<div className="rounded-lg border border-card-border bg-surface p-6 animate-fade-in">
			<div className="mb-4 flex flex-wrap items-center justify-between gap-3">
				<div>
					<h2 className="text-xl font-bold text-foreground">岗位池</h2>
					<p className="mt-1 text-xs text-text-tertiary">集中查看已采集岗位、AI 分数、状态和详情入口。</p>
				</div>
				<div className="flex items-center gap-2">
					<Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}>
						<RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
						{refreshing ? '...' : '刷新'}
					</Button>
					<Button
						size="sm"
						disabled={pendingCount === 0 || Boolean(activeTask) || starting}
						onClick={startBatchScoring}
					>
						{starting || scoring
							? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
							: <BrainCircuit className="mr-1.5 h-3.5 w-3.5" />}
						{scoring ? `评分中(${pendingCount})` : `批量评分(${pendingCount})`}
					</Button>
					<Button
						variant="secondary"
						size="sm"
						disabled={missingCompanyInfoCount === 0 || Boolean(activeTask) || starting}
						onClick={startCompanyEnrichment}
					>
						{enriching && <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
						{enriching ? '补全中' : `补全企业(${missingCompanyInfoCount})`}
					</Button>
					<BriefcaseBusiness className="h-5 w-5 text-primary" />
				</div>
			</div>
			<ImportJobPanel activeTask={activeTask} refresh={refresh} />
			{notice && <div className="mb-4 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-primary">{notice}</div>}
			{activeTask && !scoring && (
				<div className="mb-4 rounded-lg border border-card-border bg-surface-raised px-4 py-3 text-xs text-text-tertiary">
					当前正在运行 "{activeTask.label}"，完成后才能启动岗位池操作。
				</div>
			)}
			<div className="mb-4 rounded-lg border border-card-border bg-surface-raised p-3">
				<div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-card-border pb-3">
					<p className="text-xs text-text-tertiary">已选择 {selectedGreetingIds.size} 个可重新联系的岗位</p>
					<Button
						variant="secondary"
						size="sm"
						disabled={selectedGreetingIds.size === 0 || Boolean(activeTask) || Boolean(regeneratingJobId)}
						onClick={regenerateSelectedGreetings}
					>
						<RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${regeneratingJobId === 'batch' ? 'animate-spin' : ''}`} />
						批量重新生成 ({selectedGreetingIds.size})
					</Button>
				</div>
				<div className="flex flex-wrap items-center gap-2">
					<span className="mr-1 text-xs text-text-tertiary">筛选：</span>
					<button
						type="button"
						onClick={() => setStatusFilter('all')}
						className={`rounded-lg border px-2.5 py-1 text-xs transition-all ${
							statusFilter === 'all'
								? 'border-primary/40 bg-primary/15 text-primary'
								: 'border-card-border bg-surface text-text-tertiary hover:border-primary/20 hover:text-foreground'
						}`}
						>
							全部 {jobsTotal}
						</button>
					{statusOptions.map(status => (
						<button
							key={status}
							type="button"
							onClick={() => setStatusFilter(status)}
							className={`rounded-lg border px-2.5 py-1 text-xs transition-all ${
								statusFilter === status
									? 'border-primary/40 bg-primary/15 text-primary'
									: 'border-card-border bg-surface text-text-tertiary hover:border-primary/20 hover:text-foreground'
							}`}
						>
							{getStatusLabel(status)} {statusCounts[status]}
						</button>
					))}
				</div>
				<p className="mt-2 text-xs text-text-tertiary">
					{jobs.length} / {jobsTotal} 条记录
				</p>
			</div>
			<JobsTable
				jobs={jobs}
				onRegenerateGreeting={regeneratePoolGreeting}
				regeneratingJobId={regeneratingJobId}
				actionsDisabled={Boolean(activeTask)}
				selectedJobIds={selectedGreetingIds}
				onToggleJob={toggleGreetingJob}
				onToggleAll={toggleAllGreetingJobs}
			/>
		</div>
	)
}

type MonitorFilter = 'pending' | 'resume' | 'follow_up' | 'replied' | 'rejected'
const REPLY_RESOLUTION_ACTIONS = ['reply_dismissed', 'replied', 'auto_replied']

function uniqueLatestByJob(items: HistoryItem[]) {
	const seen = new Set<string>()
	return items.filter(item => {
		const key = item.job_id || `${item.company}-${item.title}-${item.action}`
		if (seen.has(key)) return false
		seen.add(key)
		return true
	})
}

function sameHistoryJob(left: HistoryItem, right: HistoryItem) {
	if (left.job_id && right.job_id) return left.job_id === right.job_id
	return left.company === right.company && left.title === right.title
}

function isReplyPendingResolved(item: HistoryItem, history: HistoryItem[]) {
	return history.some(candidate =>
		candidate.id !== item.id
		&& sameHistoryJob(item, candidate)
		&& REPLY_RESOLUTION_ACTIONS.includes(candidate.action)
		&& candidate.created_at >= item.created_at
	)
}

function isResumeFailureResolved(item: HistoryItem, history: HistoryItem[]) {
	return Boolean(item.resolved || item.resume_path) || history.some(candidate =>
		candidate.id > item.id
		&& sameHistoryJob(item, candidate)
		&& (candidate.action === 'needs_resume' || candidate.action === 'resume_sent')
	)
}

function latestHrText(item: HistoryItem) {
	const parsed = parseHistoryDetail(item)
	const latestHr = [...parsed.conversationTail].reverse().find(message => message.sender === 'hr' && message.text.trim())
	return parsed.hrQuestion || latestHr?.text || ''
}

function MonitorExecutionView({ history, refresh }: { history: HistoryItem[]; refresh: () => Promise<void> }) {
	const pendingReplies = uniqueLatestByJob(history.filter(item =>
		item.action === 'reply_pending' && !isReplyPendingResolved(item, history)
	))
	const resumeFailures = uniqueLatestByJob(history.filter(item =>
		item.action === 'resume_failed' && !isResumeFailureResolved(item, history)
	))
	const pendingItems = uniqueLatestByJob(
		[...pendingReplies, ...resumeFailures].sort((left, right) => right.id - left.id)
	)
	const resumeRequests = uniqueLatestByJob(history.filter(item =>
		item.action === 'needs_resume' || item.action === 'resume_sent' || item.action === 'resume_failed'
	))
	const followUpRecords = uniqueLatestByJob(history.filter(item => item.action === 'follow_up_sent'))
	const repliedRecords = uniqueLatestByJob(history.filter(item =>
		item.action === 'replied' || item.action === 'auto_replied'
	))
	const rejectedRecords = uniqueLatestByJob(history.filter(item => item.action === 'rejected'))
	const [activeMonitorFilter, setActiveMonitorFilter] = useState<MonitorFilter>('pending')
	const visibleHistory = activeMonitorFilter === 'resume'
		? resumeRequests
		: activeMonitorFilter === 'follow_up'
			? followUpRecords
			: activeMonitorFilter === 'replied'
				? repliedRecords
				: activeMonitorFilter === 'rejected'
					? rejectedRecords
					: pendingItems
	const displayedHistory = activeMonitorFilter === 'pending' || activeMonitorFilter === 'resume'
		? visibleHistory
		: visibleHistory.slice(0, 8)
	const [replyDrafts, setReplyDrafts] = useState<Record<number, string>>({})
	const [replySuggestions, setReplySuggestions] = useState<Record<number, string>>({})
	const [refiningReplyId, setRefiningReplyId] = useState<number | null>(null)
	const [notice, setNotice] = useState('')

	const draftFor = (item: HistoryItem) => {
		const parsed = parseHistoryDetail(item)
		return replyDrafts[item.id] ?? parsed.aiReply ?? item.detail ?? ''
	}

	const pollReplyTask = async (taskId: string) => {
		// 回复是短任务，一般在几秒内结束；轮询最长约 10 秒，超时交给全局刷新兜底。
		for (let i = 0; i < 20; i++) {
			await new Promise(r => setTimeout(r, 500))
			try {
				const res = await fetch('/api/workbench')
				const data = await res.json()
				const found = (data.tasks || []).find((t: WorkbenchTask) => t.id === taskId)
				if (!found) continue
				if (found.status === 'completed') {
					setNotice('回复已发送。')
					await refresh()
					return
				}
				if (found.status === 'failed' || found.status === 'stopped') {
					setNotice(found.error || found.stop_reason || '回复任务失败，请重试。')
					await refresh()
					return
				}
			} catch {
				// 轮询请求偶发失败则继续等待，最后走 refresh 兜底
			}
		}
		await refresh()
	}

	const sendManualReply = async (item: HistoryItem) => {
		try {
			const res = await fetch(`/api/history/${item.id}/reply`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ message: draftFor(item) }),
			})
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '回复失败')
			}
			const task = await res.json().catch(() => ({})) as Partial<WorkbenchTask>
			if (task.status === 'stopped') {
				setNotice(task.stop_reason || '回复任务未启动，请稍后重试。')
				return
			}
			setNotice('已确认回复，正在打开对应聊天会话并发送。')
			if (task.id) void pollReplyTask(task.id)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '回复失败')
		}
	}

	const refineReply = async (item: HistoryItem) => {
		const suggestion = (replySuggestions[item.id] || '').trim()
		if (!suggestion) {
			setNotice('请先填写希望 AI 如何修改这条回复。')
			return
		}
		setRefiningReplyId(item.id)
		try {
			const res = await fetch(`/api/history/${item.id}/refine`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					current_reply: draftFor(item),
					suggestion,
				}),
			})
			const data = await res.json().catch(() => ({}))
			if (!res.ok) throw new Error(data.error || 'AI 优化失败')
			setReplyDrafts(prev => ({ ...prev, [item.id]: data.message || draftFor(item) }))
			setReplySuggestions(prev => ({ ...prev, [item.id]: '' }))
			setNotice('已根据修改建议优化，请确认或继续编辑后再发送。')
		} catch (err) {
			setNotice(err instanceof Error ? err.message : 'AI 优化失败')
		} finally {
			setRefiningReplyId(null)
		}
	}

	const dismissPendingReply = async (item: HistoryItem) => {
		if (!window.confirm('确定放弃这条待回复建议吗？')) return
		try {
			const res = await fetch(`/api/history/${item.id}/dismiss`, { method: 'POST' })
			if (!res.ok) {
				const data = await res.json().catch(() => ({}))
				throw new Error(data.error || '放弃失败')
			}
			await refresh()
			setNotice('已放弃这条待回复建议。')
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '放弃失败')
		}
	}

	return (
		<div className="rounded-lg border border-card-border bg-surface p-6 animate-fade-in">
			<div className="mb-4 flex items-start justify-between gap-4">
				<div>
					<h2 className="text-xl font-bold text-foreground">监测执行</h2>
					<p className="mt-1 text-xs text-text-tertiary">处理监测发现的 HR 问题、回复建议和结果。</p>
				</div>
				<span className="rounded-lg border border-primary/30 bg-primary/10 px-3 py-1.5 text-xs text-primary">
					{pendingItems.length} 项待处理
				</span>
			</div>
			<div className="mb-4 flex flex-wrap gap-2">
				{[
					{ key: 'pending' as const, label: '待处理', count: pendingItems.length },
					{ key: 'resume' as const, label: '简历请求', count: resumeRequests.length },
					{ key: 'follow_up' as const, label: '自动跟进', count: followUpRecords.length },
					{ key: 'replied' as const, label: '已回复', count: repliedRecords.length },
					{ key: 'rejected' as const, label: '已拒绝', count: rejectedRecords.length },
				].map(item => {
					const active = activeMonitorFilter === item.key
					return (
						<button
							key={item.key}
							type="button"
							onClick={() => setActiveMonitorFilter(item.key)}
							className={`rounded-lg border px-3 py-1 text-xs transition-all ${
								active
									? 'border-primary/40 bg-primary/15 text-primary'
									: 'border-card-border text-text-tertiary hover:border-primary/20 hover:text-foreground'
							}`}
						>
							{item.label} {item.count}
						</button>
					)
				})}
			</div>
			{notice && <div className="mb-3 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-primary">{notice}</div>}
			<div className="space-y-3">
				{displayedHistory.map((item, index) => {
					const canReply = item.action === 'reply_pending'
					const isFollowUp = item.action === 'follow_up_sent'
					const isResumeFailure = item.action === 'resume_failed'
					const isResumeRequest = item.action === 'needs_resume' || item.action === 'resume_sent' || isResumeFailure
					const isReplied = item.action === 'replied' || item.action === 'auto_replied'
					const parsed = parseHistoryDetail(item)
					const hrText = latestHrText(item)
					const isLegacyReplied = item.action === 'replied' && parsed.schema === 'legacy_text'
					const hasGeneratedReply = Boolean(parsed.aiReply) && !isLegacyReplied
					const showReplyContent = canReply || Boolean(parsed.hrQuestion) || hasGeneratedReply || isResumeRequest || isReplied
					const aiReplyText = parsed.aiReply || item.detail || getActionLabel(item.action)
					const systemFailureReason = parsed.systemReason || (isResumeFailure ? '未获得更具体的错误信息。' : '')
					return (
						<div key={`${item.created_at}-${index}`} className="grid gap-3 rounded-lg border border-card-border bg-surface-raised p-4 lg:grid-cols-[120px_1fr_140px]">
							<div className="text-xs text-text-tertiary">
								<div>{formatLocalDateTime(item.created_at)}</div>
								<div className="mt-2 rounded-lg border border-primary/20 bg-primary/10 px-2 py-1 text-center font-medium text-primary text-[11px]">{getActionLabel(item.action)}</div>
							</div>
							<div>
								<div className="font-semibold text-foreground text-sm">{item.company || '岗位'}｜{item.title || '监测记录'}</div>
								{showReplyContent ? (
									<div className="mt-3 space-y-3">
										{parsed.resumeRequested && (
											<div className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-sm font-medium text-warning">
												HR 要简历，请使用你现有的简历手动发送；系统不会生成或修改简历。
											</div>
										)}
										{(isFollowUp || hrText) && (
											<div>
												<div className="text-xs text-text-tertiary mb-1">{isFollowUp ? '跟进信息' : 'HR 问题'}</div>
												<p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-text-secondary">
													{isFollowUp ? 'HR 超过设定时间未回复，系统已自动执行一次跟进。' : hrText}
												</p>
											</div>
										)}
										{isResumeFailure && (
											<div className="rounded-lg border border-danger/30 bg-danger/5 p-3">
												<div className="text-xs text-danger mb-1">错误详情</div>
												<p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-danger">{systemFailureReason}</p>
											</div>
										)}
										{canReply ? (
											<div className="space-y-3">
												<div className="mb-1 text-xs text-text-tertiary">AI 建议回复</div>
												<textarea
													value={draftFor(item)}
													onChange={event => setReplyDrafts(prev => ({ ...prev, [item.id]: event.target.value }))}
													className="min-h-[92px] w-full rounded-lg border border-card-border bg-surface p-3 text-sm leading-6 text-foreground outline-none focus:border-primary/40 focus:ring-2 focus:ring-primary/15 transition-colors"
												/>
												<div className="rounded-lg border border-card-border bg-surface/70 p-3">
													<div className="mb-2 text-xs text-text-tertiary">给 AI 的修改建议</div>
													<textarea
														value={replySuggestions[item.id] || ''}
														onChange={event => setReplySuggestions(prev => ({ ...prev, [item.id]: event.target.value }))}
														placeholder="例如：更简洁一些；突出我做过制造业 Agent 项目；不要主动约时间"
														className="min-h-[68px] w-full rounded-lg border border-card-border bg-surface-raised p-3 text-sm leading-6 text-foreground outline-none focus:border-primary/40 focus:ring-2 focus:ring-primary/15 transition-colors"
													/>
													<div className="mt-2 flex justify-end">
														<Button
															variant="secondary"
															size="sm"
															disabled={refiningReplyId === item.id || !(replySuggestions[item.id] || '').trim()}
															onClick={() => refineReply(item)}
														>
															<Sparkles className="mr-1.5 h-3.5 w-3.5" />
															{refiningReplyId === item.id ? '优化中…' : '根据建议优化'}
														</Button>
													</div>
												</div>
											</div>
										) : isResumeRequest || !hasGeneratedReply ? null : (
											<div className="rounded-lg border border-card-border bg-surface p-3 greeting-block">
												<div className="text-xs text-text-tertiary mb-1">AI 回复</div>
												<p className="whitespace-pre-wrap text-sm leading-6 text-text-secondary">{aiReplyText}</p>
											</div>
										)}
									</div>
								) : (
									<p className="mt-2 text-sm leading-6 text-text-tertiary">{item.detail || getActionLabel(item.action)}</p>
								)}
								{canReply ? (
									<p className="mt-2 text-xs text-primary">AI 建议：需要人工确认后再回复。</p>
								) : item.action === 'needs_resume' ? (
									<p className="mt-2 text-xs text-primary">监测发现 HR 要简历，已生成定制简历。</p>
								) : item.action === 'resume_sent' ? (
									<p className="mt-2 text-xs text-success">定制简历已生成并标记发送。</p>
								) : isResumeFailure ? (
									<p className="mt-2 text-xs text-danger">简历生成失败，请手动处理。</p>
								) : item.action === 'rejected' ? (
									<p className="mt-2 text-xs text-danger">HR 已拒绝，该岗位已标记为拒绝状态。</p>
								) : isReplied ? (
										<p className="mt-2 text-xs text-success">HR 已反馈或系统已完成回复。</p>
								) : null}
							</div>
							<div className="grid gap-2">
								<Button size="sm" disabled={!canReply} onClick={() => sendManualReply(item)}><MessageCircle className="mr-1.5 h-3.5 w-3.5" />确认</Button>
								<Button variant="secondary" size="sm" disabled={!canReply} onClick={() => setNotice('可直接在“AI 建议回复”文本框中修改。')}>编辑</Button>
								<Button variant="secondary" size="sm" disabled={!canReply} onClick={() => dismissPendingReply(item)}>放弃</Button>
							</div>
						</div>
					)
				})}
				{!visibleHistory.length && <div className="rounded-lg border border-dashed border-card-border bg-surface-raised p-6 text-center text-xs text-text-tertiary">暂无待处理事项</div>}
			</div>
		</div>
	)
}
