import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useDashboard, type HistoryItem, type Job, type PipelineStage, type WorkbenchTask } from '@/hooks/useDashboard'
import { Button } from '@/components/ui/button'
import { CityMultiSelect } from '@/components/ui/city-multi-select'
import { useConfirm } from '@/components/confirm-dialog'
import { JobsTable } from '@/components/dashboard/JobsTable'
import { ImportJobPanel } from '@/components/dashboard/ImportJobPanel'
import { parseHistoryDetail } from '@/lib/historyDetail'
import { getActionLabel, getStatusLabel, STATUS_LABELS } from '@/lib/status'
import { formatLocalDateTime } from '@/lib/utils'
import { BrainCircuit, Check, ChevronLeft, ChevronRight, CircleDot, Clock3, Download, ListChecks, MessageCircle, Play, RefreshCw, Search, ShieldCheck, Sparkles, Square, X } from 'lucide-react'

type WorkbenchMode = 'full' | 'collect' | 'score' | 'monitor'
type DashboardView = 'workbench' | 'jobs' | 'monitor'

/** 阶段 key 由后端显式下发，顺序即流程条顺序。 */
const PIPELINE_STAGE_ORDER: PipelineStage[] = ['collect', 'score', 'confirm', 'greet', 'send']

const PIPELINE_STAGE_LABELS: Record<PipelineStage, string> = {
	collect: '采集岗位',
	score: 'AI 评分',
	confirm: '人工确认',
	greet: '生成招呼语',
	send: '发送与监测',
}

function taskStatusText(status: string) {
	if (status === 'failed') return '运行失败'
	if (status === 'completed') return '已完成'
	if (status === 'stopped') return '已停止'
	if (status === 'stopping') return '停止中'
	return '运行中'
}

/** 终态任务（失败/停止/完成）的说明文案，失败时优先展示具体原因。 */
function taskOutcomeText(task: WorkbenchTask) {
	if (task.status === 'failed') return task.error || '任务执行失败，请检查浏览器连接与 AI 配置后重试。'
	if (task.status === 'stopped') return task.stop_reason || '任务已停止。'
	return ''
}

function taskStatusClass(status: string) {
	if (status === 'failed') return 'border-danger/30 bg-danger/5'
	if (status === 'completed' || status === 'stopped') return 'border-card-border bg-surface'
	return 'border-primary/30 bg-primary/5'
}

/** 任务当前阶段的展示文案；后端未下发 stage 时返回空串，由调用方回退到原始日志。 */
function taskStageLabel(task: WorkbenchTask) {
	return task.stage ? PIPELINE_STAGE_LABELS[task.stage] ?? '' : ''
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

// 某些分组只在有内容时才渲染，流程条点击的锚点缺失时退回该步骤下最接近的分组。
const QUEUE_ANCHOR_FALLBACKS: Record<string, string[]> = {
	'queue-send': ['queue-send-errors', 'queue-greeting'],
	'queue-greeting': ['queue-send-errors', 'queue-send'],
	'queue-confirm': ['queue-greeting'],
}

interface PipelineStepView {
	key: PipelineStage
	label: string
	/** 待处理数量；0 表示这一步当前没有需要用户参与的事项。 */
	count: number
	/** 数量的语义，避免只给一个光秃秃的数字。 */
	countLabel: string
	/** 点击后跳转的锚点 id；为空表示该步不可点。 */
	anchor?: string
	/** 该步是否必须由人工介入才能推进。 */
	manual: boolean
}

interface CollectionOptions {
	keywords: string[]
	cities: string[]
	limit: number
}

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
	const confirm = useConfirm()
	// 岗位池状态筛选上移到这里，便于透传给 useDashboard 触发服务端按页请求。
	const [jobsStatusFilter, setJobsStatusFilter] = useState('all')
	const [jobsCompanyFilter, setJobsCompanyFilter] = useState('')
	const [jobsPage, setJobsPage] = useState(1)
	const [jobsPageSize, setJobsPageSize] = useState(50)
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
	} = useDashboard(view, jobsStatusFilter, jobsCompanyFilter, jobsPage, jobsPageSize)

	useEffect(() => {
		const totalPages = Math.max(1, Math.ceil(jobsTotal / jobsPageSize))
		if (jobsPage > totalPages) setJobsPage(totalPages)
	}, [jobsPage, jobsPageSize, jobsTotal])
	const [selected, setSelected] = useState<string[]>([])
	const [notice, setNotice] = useState('')
	const [preflightMessages, setPreflightMessages] = useState<string[]>([])
	const [selectedJob, setSelectedJob] = useState<Job | null>(null)
	const [modePending, setModePending] = useState<WorkbenchMode | null>(null)
	const [actionPending, setActionPending] = useState<string | null>(null)
	const [collectionKeywords, setCollectionKeywords] = useState('')
	const [collectionCities, setCollectionCities] = useState<string[]>([])
	const [collectionLimit, setCollectionLimit] = useState(100)
	const [collectionConfigLoading, setCollectionConfigLoading] = useState(true)

	useEffect(() => {
		let cancelled = false
		void fetch('/api/config')
			.then(response => response.ok ? response.json() : Promise.reject(new Error('配置加载失败')))
			.then(config => {
				if (cancelled) return
				setCollectionKeywords((config.search?.keywords || []).join('、'))
				setCollectionCities(config.search?.cities?.length ? config.search.cities : config.profile?.target_cities || [])
				const maxPages = Number(config.search?.max_pages || 3)
				setCollectionLimit(Math.max(20, Math.min(maxPages * 50, 500)))
			})
			.catch(() => {
				if (!cancelled) setNotice('未能读取采集配置，请填写关键词和城市后再启动。')
			})
			.finally(() => {
				if (!cancelled) setCollectionConfigLoading(false)
			})
		return () => { cancelled = true }
	}, [])

	// 队列以服务端状态为唯一来源。确认后的岗位会从 ready 变为 approved，
	// 随后立即出现在待发送队列，不能再用本地 ID 集合把它过滤掉。
	const todayJobs = workbench.pending_confirmation
	const pendingGreetingJobs = workbench.pending_greetings
	const needsGreetingJobs = workbench.needs_greeting
	const activeTask = workbench.task
	const visibleTask = activeTask || workbench.last_task
	const pendingReplies = history.filter(item => item.action === 'reply_pending')

	const getTaskStopReason = (task: Partial<WorkbenchTask>) => (
		task.stop_reason || task.error || '任务未启动，请检查发送时间窗口后重试。'
	)

	const toggleJob = (id: string) => {
		setSelected(prev => (prev.includes(id) ? prev.filter(item => item !== id) : [...prev, id]))
	}

	const runPreflight = async (mode: WorkbenchMode, options?: CollectionOptions) => {
		const params = new URLSearchParams({ mode })
		if (options?.keywords.length) params.set('keywords', options.keywords.join(','))
		if (options?.cities.length) params.set('cities', options.cities.join(','))
		const res = await fetch(`/api/workbench/preflight?${params.toString()}`)
		const data = await parsePreflightResponse(res)
		setPreflightMessages(data.messages)
		if (!data.ok) {
			setNotice('请按提示处理后再启动')
			return false
		}
		return true
	}

	const handleModeClick = async (mode: WorkbenchMode, collectionOptions?: CollectionOptions) => {
		if (modePending) return
		try {
			if (activeTask?.mode === mode) {
				const confirmed = await confirm({
					title: `停止「${activeTask.label}」`,
					description: '会在下一个安全检查点退出，正在进行的单次浏览器操作或 AI 请求可能先完成。',
					confirmLabel: '停止任务',
					cancelLabel: '继续运行',
					will: ['不再启动新的步骤', '已入库岗位与已生成内容保留'],
					wont: ['不会删除已完成的数据'],
				})
				if (confirmed) {
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
			if (!(await runPreflight(mode, collectionOptions))) return
			setNotice(`${target?.title || '任务'}启动中，请稍候...`)
			const task = await startTask(mode, collectionOptions)
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
		const confirmed = await confirm({
			title: `确认 ${count} 个岗位`,
			description: '确认后岗位会进入“待发送”队列，你仍需单独点击“发送招呼语”。',
			confirmLabel: `确认 ${count} 个岗位`,
			cancelLabel: '暂不确认',
			will: [`将 ${count} 个岗位加入待发送队列`, '保留招呼语供发送前检查'],
			wont: ['不会立即联系 HR', '不会自动回复 HR', '不会上传或发送简历'],
		})
		if (!confirmed) return
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
			await refresh()
			setNotice(`已确认 ${count} 个岗位，请在“待发送”项中点击“发送招呼语”。`)
			setSelected(prev => prev.filter(id => !new Set(ids).has(id)))
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '投递失败')
		} finally {
			setActionPending(null)
		}
	}

	const rejectSelectedJobs = async (ids: string[]): Promise<boolean> => {
		if (!ids.length) return false
		const count = ids.length
		const confirmed = await confirm({
			title: `放弃 ${count} 个岗位`,
			description: '放弃后这些岗位不会进入投递流程。',
			confirmLabel: `放弃 ${count} 个岗位`,
			cancelLabel: '保留',
			tone: 'danger',
			will: ['岗位状态标记为已拒绝，可在岗位池中查看'],
			wont: ['不会向 HR 发送任何消息'],
		})
		if (!confirmed) return false
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
			await refresh()
			setNotice(`已放弃 ${count} 个岗位。`)
			return true
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '放弃失败')
			return false
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

	// 返回是否真的提交了任务：用户取消确认时返回 false，调用方据此决定要不要清空勾选。
	const regenerateGreetings = async (ids: string[]): Promise<boolean> => {
		if (!ids.length) return false
		const confirmed = await confirm({
			title: `重新生成 ${ids.length} 条招呼语`,
			description: '生成后仍需你在工作台确认才会发送。',
			confirmLabel: `重新生成 ${ids.length} 条`,
			will: ['覆盖现有的招呼语草稿'],
			wont: ['不会自动发送到 BOSS'],
		})
		if (!confirmed) return false
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
			return true
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '重新生成失败')
			return false
		} finally {
			setActionPending(null)
		}
	}

	const regenerateGreeting = async (jobId: string) => {
		const confirmed = await confirm({
			title: '重新生成这条招呼语',
			confirmLabel: '重新生成',
			will: ['覆盖现有的招呼语草稿'],
			wont: ['不会自动发送到 BOSS'],
		})
		if (!confirmed) return
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
				setStatusFilter={status => {
					setJobsStatusFilter(status)
					setJobsPage(1)
				}}
				companyFilter={jobsCompanyFilter}
				setCompanyFilter={company => {
					setJobsCompanyFilter(company)
					setJobsPage(1)
				}}
				page={jobsPage}
				pageSize={jobsPageSize}
				setPage={setJobsPage}
				setPageSize={size => {
					setJobsPageSize(size)
					setJobsPage(1)
				}}
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

	const parseCollectionList = (value: string) => value
		.split(/[、,，\n]/)
		.map(item => item.trim())
		.filter(Boolean)
	// 采集相关的模式（全流程 / 仅采集）共用同一套参数校验，避免提示文案分叉。
	const collectOptions = (): CollectionOptions | null => {
		const keywords = parseCollectionList(collectionKeywords)
		const cities = collectionCities
		if (!keywords.length || !cities.length) {
			setNotice(!keywords.length ? '请至少填写一个岗位关键词。' : '请至少填写一个目标城市。')
			return null
		}
		return { keywords, cities, limit: collectionLimit }
	}
	const startCollection = () => {
		const options = collectOptions()
		if (options) void handleModeClick('full', options)
	}
	const startCollectOnly = () => {
		const options = collectOptions()
		if (options) void handleModeClick('collect', options)
	}

	return <WorkbenchControlCenter
		workbench={workbench}
		history={history}
		activeTask={activeTask}
		visibleTask={visibleTask}
		todayJobs={todayJobs}
		pendingGreetingJobs={pendingGreetingJobs}
		needsGreetingJobs={needsGreetingJobs}
		pendingReplies={pendingReplies}
		selected={selected}
		setSelected={setSelected}
		toggleJob={toggleJob}
		collectionKeywords={collectionKeywords}
		setCollectionKeywords={setCollectionKeywords}
		collectionCities={collectionCities}
		setCollectionCities={setCollectionCities}
		collectionLimit={collectionLimit}
		setCollectionLimit={setCollectionLimit}
		collectionConfigLoading={collectionConfigLoading}
		pendingCount={pendingCount}
		startCollectOnly={startCollectOnly}
		modePending={modePending}
		actionPending={actionPending}
		notice={notice}
		error={error}
		preflightMessages={preflightMessages}
		refreshing={refreshing}
		refresh={refresh}
		startCollection={startCollection}
		handleModeClick={handleModeClick}
		confirmDeliver={confirmDeliver}
		rejectSelectedJobs={rejectSelectedJobs}
		sendReadyGreetings={sendReadyGreetings}
		regenerateGreetings={regenerateGreetings}
		regenerateGreeting={regenerateGreeting}
		openJobDetail={openJobDetail}
		downloadResume={downloadResume}
		markResumeSent={markResumeSent}
		selectedJob={selectedJob}
		setSelectedJob={setSelectedJob}
	/>

}

interface WorkbenchControlCenterProps {
	workbench: ReturnType<typeof useDashboard>['workbench']
	history: HistoryItem[]
	activeTask: WorkbenchTask | null
	visibleTask: WorkbenchTask | null
	todayJobs: Job[]
	pendingGreetingJobs: Job[]
	needsGreetingJobs: Job[]
	pendingReplies: HistoryItem[]
	selected: string[]
	setSelected: (ids: string[]) => void
	toggleJob: (id: string) => void
	collectionKeywords: string
	setCollectionKeywords: (value: string) => void
	collectionCities: string[]
	setCollectionCities: (value: string[]) => void
	collectionLimit: number
	setCollectionLimit: (value: number) => void
	collectionConfigLoading: boolean
	/** 待 AI 评分的岗位数（status=pending），用于流程条。 */
	pendingCount: number
	modePending: WorkbenchMode | null
	actionPending: string | null
	notice: string
	error: string
	preflightMessages: string[]
	refreshing: boolean
	refresh: () => Promise<void>
	startCollection: () => void
	startCollectOnly: () => void
	handleModeClick: (mode: WorkbenchMode) => Promise<void>
	confirmDeliver: (ids: string[]) => Promise<void>
	rejectSelectedJobs: (ids: string[]) => Promise<boolean>
	sendReadyGreetings: (ids: string[]) => Promise<void>
	regenerateGreetings: (ids: string[]) => Promise<boolean>
	regenerateGreeting: (id: string) => Promise<void>
	openJobDetail: (job: Job) => Promise<void>
	downloadResume: (job: Job) => void
	markResumeSent: (job: Job) => Promise<void>
	selectedJob: Job | null
	setSelectedJob: (job: Job | null) => void
}

function WorkbenchControlCenter(props: WorkbenchControlCenterProps) {
	const confirm = useConfirm()
	const {
		workbench, activeTask, visibleTask, todayJobs, pendingGreetingJobs, needsGreetingJobs, pendingReplies,
		selected, setSelected, toggleJob, collectionKeywords, setCollectionKeywords, collectionCities,
		setCollectionCities, collectionLimit, setCollectionLimit,
		collectionConfigLoading, pendingCount, modePending, actionPending, notice, error, preflightMessages, refreshing,
		refresh, startCollection, startCollectOnly, handleModeClick, confirmDeliver, rejectSelectedJobs,
		sendReadyGreetings, regenerateGreetings,
		regenerateGreeting, openJobDetail, downloadResume, markResumeSent, selectedJob, setSelectedJob,
	} = props
	const [selectedGreetingIds, setSelectedGreetingIds] = useState<string[]>([])
	// 「待生成招呼语」与「待发送」是两组独立队列，各自维护勾选，避免互相串号。
	const [selectedNeedsGreetingIds, setSelectedNeedsGreetingIds] = useState<string[]>([])
	const queueTotal = todayJobs.length + pendingGreetingJobs.length + needsGreetingJobs.length
		+ workbench.needs_resume.length + workbench.send_errors.length + pendingReplies.length
	// 流程条的数字全部来自"待处理队列"，与下方"需要你处理"同源，
	// 不再使用历史累计漏斗（那会把"曾经有过结果"渲染成"当前待办"）。
	const sendStageCount = pendingGreetingJobs.length + workbench.send_errors.length
		+ workbench.needs_resume.length + pendingReplies.length
	const activeStageIndex = activeTask?.stage ? PIPELINE_STAGE_ORDER.indexOf(activeTask.stage) : -1
	const pipelineSteps: PipelineStepView[] = [
		{ key: 'collect', label: PIPELINE_STAGE_LABELS.collect, count: Number(workbench.funnel['采集总数'] || 0), countLabel: '库内岗位', manual: false },
		{ key: 'score', label: PIPELINE_STAGE_LABELS.score, count: pendingCount, countLabel: '待评分', anchor: '/jobs', manual: false },
		{ key: 'confirm', label: PIPELINE_STAGE_LABELS.confirm, count: todayJobs.length, countLabel: '待确认', anchor: 'queue-confirm', manual: true },
		{ key: 'greet', label: PIPELINE_STAGE_LABELS.greet, count: needsGreetingJobs.length, countLabel: '待生成', anchor: 'queue-greeting', manual: false },
		{ key: 'send', label: PIPELINE_STAGE_LABELS.send, count: sendStageCount, countLabel: '待处理', anchor: 'queue-send', manual: true },
	]
	const pipelineStepState = (index: number): 'idle' | 'active' | 'done' => {
		// 只有任务真正在跑时才谈"走到第几步"；空闲时流程条只表达待办，不假装有进度。
		if (!activeTask || activeStageIndex < 0) return 'idle'
		if (index === activeStageIndex) return 'active'
		return index < activeStageIndex ? 'done' : 'idle'
	}
	const scrollToQueue = (anchor: string) => {
		const target = [anchor, ...(QUEUE_ANCHOR_FALLBACKS[anchor] ?? [])]
			.map(id => document.getElementById(id))
			.find(Boolean)
		target?.scrollIntoView({ behavior: 'smooth', block: 'start' })
	}
	// 失败/停止的最近任务提供原地重试；仅限工作台直接支持的模式，
	// 其余模式（deliver/reply/import 等）由用户回到对应入口重新发起。
	const retryableMode = (mode: string): WorkbenchMode | null =>
		(['full', 'collect', 'score', 'monitor'] as WorkbenchMode[]).includes(mode as WorkbenchMode)
			? mode as WorkbenchMode
			: null
	// 只在没有任务运行时回看上一次的结果，避免和正在跑的进度互相干扰。
	const lastOutcome = visibleTask && !activeTask ? taskOutcomeText(visibleTask) : ''
	const lastOutcomeFailed = !activeTask && visibleTask?.status === 'failed'
	const retryMode = visibleTask && !activeTask ? retryableMode(visibleTask.mode) : null
	// 运行记录的主标题：运行中看当前阶段，结束后看停在哪一步。
	// 后端没下发 stage 时退回最后一条原始日志，不向用户暴露"等待后端返回阶段"这类内部状态。
	const visibleStageLabel = visibleTask ? taskStageLabel(visibleTask) : ''
	const lastLog = visibleTask?.logs.length ? visibleTask.logs[visibleTask.logs.length - 1] : ''
	const runLogHeadline = activeTask
		? (visibleStageLabel || lastLog || '任务启动中…')
		: visibleStageLabel
			? (visibleTask?.status === 'completed' ? `完成于「${visibleStageLabel}」` : `停在「${visibleStageLabel}」`)
			: lastLog
	const toggleGreeting = (id: string) => {
		setSelectedGreetingIds(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
	}
	const toggleNeedsGreeting = (id: string) => {
		setSelectedNeedsGreetingIds(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
	}
	const generateSelectedGreetings = async () => {
		if (!selectedNeedsGreetingIds.length) return
		// 生成失败时保留勾选，用户可直接重试而不必重新挑选。
		if (await regenerateGreetings(selectedNeedsGreetingIds)) setSelectedNeedsGreetingIds([])
	}
	// 取消 = 这个岗位不生成招呼语了，标记为已拒绝，从工作台队列移出、落回岗位池。
	// 复用放弃岗位的确认对话框（危险语气 + 说清不会联系 HR）。
	const cancelNeedsGreetings = async (ids: string[]) => {
		if (!ids.length) return
		if (!(await rejectSelectedJobs(ids))) return
		const cancelled = new Set(ids)
		setSelectedNeedsGreetingIds(current => current.filter(id => !cancelled.has(id)))
	}
	const cancelSelectedGreetings = () => cancelNeedsGreetings(selectedNeedsGreetingIds)
	const sendSelectedGreetings = async () => {
		if (!selectedGreetingIds.length) return
		const count = selectedGreetingIds.length
		const confirmed = await confirm({
			title: `发送 ${count} 条招呼语`,
			description: '确认后系统会通过当前浏览器会话依次联系这些岗位的 HR。',
			confirmLabel: `发送 ${count} 条`,
			cancelLabel: '暂不发送',
			will: [`发送 ${count} 条已确认的招呼语`, '遵守每日上限、发送时间窗口与随机间隔'],
			wont: ['不会发送未勾选的岗位', '不会自动回复 HR', '不会上传或发送简历'],
		})
		if (!confirmed) return
		await sendReadyGreetings(selectedGreetingIds)
		setSelectedGreetingIds([])
	}
	const rejectSelectedGreetings = async (ids: string[]) => {
		if (!ids.length) return
		const rejected = await rejectSelectedJobs(ids)
		if (!rejected) return
		const rejectedIds = new Set(ids)
		setSelectedGreetingIds(current => current.filter(id => !rejectedIds.has(id)))
	}

	return <div className="mx-auto max-w-[100rem] space-y-4 animate-fade-in">
		<header className="flex flex-wrap items-end justify-between gap-4">
			<div><h2 className="text-2xl font-bold tracking-tight text-foreground">工作台</h2><p className="mt-1.5 text-sm text-text-secondary">设置采集目标，启动任务，然后处理需要你确认的事项。</p></div>
			<div className="flex items-center gap-2">
				<span className={`inline-flex h-8 items-center gap-2 rounded-lg border px-3 text-xs font-medium ${activeTask ? 'border-primary-border bg-primary-soft text-primary' : 'border-card-border bg-surface text-text-secondary'}`}><span className={`h-1.5 w-1.5 rounded-full ${activeTask ? 'bg-primary status-pulse' : 'bg-success'}`} />{activeTask ? `${activeTask.label}运行中` : '系统就绪'}</span>
				<Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}><RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />刷新</Button>
			</div>
		</header>

		<section className="rounded-xl border border-card-border bg-surface shadow-card">
			<div className="flex items-start gap-3 border-b border-card-border px-5 py-4 sm:px-6"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary"><Search className="h-4 w-4" /></span><div><h3 className="text-base font-semibold text-foreground">采集控制台</h3><p className="mt-1 text-xs leading-5 text-text-secondary">本次参数只用于当前任务，不会覆盖配置页中的默认设置。</p></div></div>
			<div className="grid gap-4 p-5 sm:p-6 lg:grid-cols-[minmax(15rem,1.5fr)_minmax(12rem,1fr)_9rem_minmax(13rem,0.9fr)] lg:items-start">
				<WorkbenchInput label="岗位关键词"><input value={collectionKeywords} onChange={event => setCollectionKeywords(event.target.value)} disabled={collectionConfigLoading || Boolean(activeTask)} placeholder="例如：产品经理、数据分析" className="workbench-input" /></WorkbenchInput>
				<WorkbenchInput label="目标城市"><CityMultiSelect value={collectionCities} onChange={setCollectionCities} disabled={collectionConfigLoading || Boolean(activeTask)} /></WorkbenchInput>
				<WorkbenchInput label="最多采集"><input type="number" min={1} max={500} value={collectionLimit} onChange={event => setCollectionLimit(Math.max(1, Math.min(Number(event.target.value) || 1, 500)))} disabled={Boolean(activeTask)} className="workbench-input tabular-nums" /></WorkbenchInput>
				{/* pt-6 与其它列 label（16px 行高 + mb-2）对齐，items-start 让城市 chips 只撑高自己这一列 */}
				<div className="space-y-2 lg:pt-6"><Button className="h-10 w-full" disabled={collectionConfigLoading || Boolean(activeTask) || Boolean(modePending)} onClick={startCollection}><Play className="h-4 w-4" />{modePending ? '正在启动…' : '运行全流程'}</Button><p className="text-center text-xs text-text-tertiary">发送前仍需人工确认</p></div>
			</div>
			<div className="flex flex-wrap items-center gap-2 border-t border-card-border bg-surface-raised px-5 py-3 sm:px-6"><span className="mr-1 text-xs text-text-tertiary">单独运行</span><Button variant="ghost" size="sm" disabled={collectionConfigLoading || Boolean(activeTask) || Boolean(modePending)} onClick={startCollectOnly}><Search className="h-3.5 w-3.5" />仅采集</Button><Button variant="ghost" size="sm" disabled={Boolean(activeTask) || Boolean(modePending)} onClick={() => handleModeClick('score')}><BrainCircuit className="h-3.5 w-3.5" />仅评分</Button><Button variant="ghost" size="sm" disabled={Boolean(activeTask) || Boolean(modePending)} onClick={() => handleModeClick('monitor')}><MessageCircle className="h-3.5 w-3.5" />单独监测</Button><a href="/config" className="ml-auto inline-flex min-h-9 items-center gap-1 rounded-lg px-3 text-xs font-medium text-text-secondary transition hover:bg-surface hover:text-foreground">调整默认配置<ChevronRight className="h-3.5 w-3.5" /></a></div>
		</section>

		{(notice || error || preflightMessages.length > 0) && <section aria-live="polite" className={`rounded-xl border px-4 py-3 text-sm ${error || preflightMessages.length ? 'border-danger/30 bg-danger/5 text-danger' : 'border-primary-border bg-primary-softer text-primary'}`}><p>{error || notice}</p>{preflightMessages.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">{preflightMessages.map((message, index) => <li key={`${message}-${index}`}>{message}</li>)}</ul>}</section>}

		<section className="rounded-xl border border-card-border bg-surface px-5 py-5 sm:px-6">
			<div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-foreground">当前流程</h3><p className="mt-1 text-xs text-text-tertiary">数字是各环节的待办数量，点击可直接跳到对应队列；投递前必须由你确认。</p></div>{visibleTask && <span className={`rounded-lg border px-2.5 py-1 text-xs ${taskStatusClass(visibleTask.status)}`}>{taskStatusText(visibleTask.status)} · {visibleTask.label}</span>}</div>
			<ol className="grid gap-2.5 sm:grid-cols-5">{pipelineSteps.map((step, index) => <PipelineStepCard key={step.key} step={step} index={index} state={pipelineStepState(index)} onNavigate={scrollToQueue} />)}</ol>
			{lastOutcome && <div className={`mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 ${lastOutcomeFailed ? 'border-danger/30 bg-danger/5' : 'border-card-border bg-surface-raised'}`}><p className={`min-w-0 flex-1 text-xs leading-5 ${lastOutcomeFailed ? 'text-danger' : 'text-text-secondary'}`}>{lastOutcome}</p><div className="flex shrink-0 items-center gap-2">{retryMode && <Button variant="secondary" size="sm" disabled={Boolean(modePending) || Boolean(activeTask)} onClick={() => handleModeClick(retryMode)}><RefreshCw className="h-3.5 w-3.5" />重新运行</Button>}<button type="button" onClick={() => scrollToQueue('run-log')} className="inline-flex min-h-9 items-center rounded-lg px-3 text-xs font-medium text-text-secondary transition hover:bg-surface hover:text-foreground">查看日志</button></div></div>}
		</section>

		<div className="grid items-start gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(18rem,1fr)]">
			<section className="overflow-hidden rounded-xl border border-card-border bg-surface">
				<div className="flex flex-wrap items-center justify-between gap-3 border-b border-card-border px-5 py-4"><div className="flex items-center gap-3"><span className="grid h-8 w-8 place-items-center rounded-lg bg-primary-soft text-primary"><ListChecks className="h-4 w-4" /></span><div><h3 className="text-base font-semibold text-foreground">需要你处理</h3><p className="mt-0.5 text-xs text-text-tertiary">{queueTotal ? `待确认 ${todayJobs.length} 项 · 待发送 ${pendingGreetingJobs.length} 项 · 全部 ${queueTotal} 项` : '当前没有待确认或待发送事项。'}</p></div></div></div>
				<div>
					{workbench.send_errors.length > 0 && <QueueGroupHeader id="queue-send-errors" title="优先处理失败" description="检查浏览器状态后再重新发送。" count={workbench.send_errors.length} />}
					<div className="divide-y divide-card-border">{workbench.send_errors.map(job => <QueueRow key={`error-${job.id}`} tone="danger" label="发送失败" job={job} detail={job.last_error || '发送失败，请检查浏览器状态后重试。'} actions={<><Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings([job.id])}>重新发送</Button><Button variant="ghost" size="sm" onClick={() => openJobDetail(job)}>查看详情</Button></>} />)}</div>
					{todayJobs.length > 0 && <QueueGroupHeader id="queue-confirm" title="先确认岗位" description="查看岗位与招呼语，确认后进入待发送队列。" count={todayJobs.length} actions={<><Button variant="ghost" size="sm" onClick={() => setSelected(selected.length === todayJobs.length ? [] : todayJobs.map(job => job.id))}>{selected.length === todayJobs.length ? '取消全选' : '全选'}</Button><Button variant="outline" size="sm" disabled={!selected.length || actionPending !== null} onClick={() => rejectSelectedJobs(selected)}>放弃选中 {selected.length}</Button><Button size="sm" disabled={!selected.length || actionPending !== null} onClick={() => confirmDeliver(selected)}>确认选中 {selected.length}</Button></>} />}
					<div className="divide-y divide-card-border">{todayJobs.map(job => <QueueRow key={`confirm-${job.id}`} tone="warning" label="待人工确认" job={job} detail={job.greeting || job.score_reason || '查看岗位详情后决定是否联系。'} selected={selected.includes(job.id)} onToggle={() => toggleJob(job.id)} actions={<><Button variant="secondary" size="sm" onClick={() => openJobDetail(job)}>查看详情</Button><Button variant="ghost" size="sm" onClick={() => regenerateGreeting(job.id)}>重生成</Button><Button variant="outline" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedJobs([job.id])}>放弃岗位</Button><Button size="sm" disabled={actionPending !== null} onClick={() => confirmDeliver([job.id])}>确认岗位</Button></>} />)}</div>
					{pendingGreetingJobs.length > 0 && <QueueGroupHeader id="queue-send" title="再发送招呼语" description="这些岗位已经确认，勾选后可批量发送。" count={pendingGreetingJobs.length} actions={<><Button variant="ghost" size="sm" onClick={() => setSelectedGreetingIds(selectedGreetingIds.length === pendingGreetingJobs.length ? [] : pendingGreetingJobs.map(job => job.id))}>{selectedGreetingIds.length === pendingGreetingJobs.length ? '取消全选' : '全选'}</Button><Button variant="outline" size="sm" disabled={!selectedGreetingIds.length || actionPending !== null} onClick={() => rejectSelectedGreetings(selectedGreetingIds)}>放弃选中 {selectedGreetingIds.length}</Button><Button size="sm" disabled={!selectedGreetingIds.length || actionPending !== null} onClick={sendSelectedGreetings}>发送选中 {selectedGreetingIds.length}</Button></>} />}
					<div className="divide-y divide-card-border">{pendingGreetingJobs.map(job => <QueueRow key={`send-${job.id}`} tone="primary" label="待发送" job={job} detail={job.greeting || '招呼语已生成，等待发送。'} selected={selectedGreetingIds.includes(job.id)} onToggle={() => toggleGreeting(job.id)} actions={<><Button variant="secondary" size="sm" onClick={() => openJobDetail(job)}>查看详情</Button><Button variant="outline" size="sm" disabled={actionPending !== null} onClick={() => rejectSelectedGreetings([job.id])}>放弃岗位</Button><Button size="sm" disabled={actionPending !== null} onClick={() => sendReadyGreetings([job.id])}>发送招呼语</Button></>} />)}</div>
					{needsGreetingJobs.length > 0 && <QueueGroupHeader id="queue-greeting" title="待生成招呼语" description="这些岗位已确认，但招呼语还没生成。勾选后可批量生成。" count={needsGreetingJobs.length} actions={<><Button variant="ghost" size="sm" onClick={() => setSelectedNeedsGreetingIds(selectedNeedsGreetingIds.length === needsGreetingJobs.length ? [] : needsGreetingJobs.map(job => job.id))}>{selectedNeedsGreetingIds.length === needsGreetingJobs.length ? '取消全选' : '全选'}</Button><Button variant="outline" size="sm" disabled={!selectedNeedsGreetingIds.length || actionPending !== null} onClick={cancelSelectedGreetings}>取消选中 {selectedNeedsGreetingIds.length}</Button><Button size="sm" disabled={!selectedNeedsGreetingIds.length || actionPending !== null} onClick={generateSelectedGreetings}>批量生成 {selectedNeedsGreetingIds.length}</Button></>} />}
					<div className="divide-y divide-card-border">
					{needsGreetingJobs.map(job => <QueueRow key={`greeting-${job.id}`} tone="info" label="待生成招呼语" job={job} detail="岗位已确认，但招呼语尚未生成。" selected={selectedNeedsGreetingIds.includes(job.id)} onToggle={() => toggleNeedsGreeting(job.id)} actions={<><Button variant="outline" size="sm" disabled={actionPending !== null} onClick={() => cancelNeedsGreetings([job.id])}>取消</Button><Button size="sm" disabled={actionPending !== null} onClick={() => regenerateGreeting(job.id)}>生成招呼语</Button></>} />)}
					</div>
					{(workbench.needs_resume.length > 0 || pendingReplies.length > 0) && <QueueGroupHeader id="queue-other" title="其他待处理" description="处理简历请求和 HR 回复。" count={workbench.needs_resume.length + pendingReplies.length} />}
					<div className="divide-y divide-card-border">
					{workbench.needs_resume.map(job => <QueueRow key={`resume-${job.id}`} tone="warning" label="HR 要简历" job={job} detail="请下载定制简历，并在 BOSS 中手动发送给 HR。" actions={<><Button size="sm" onClick={() => downloadResume(job)}><Download className="h-3.5 w-3.5" />下载简历</Button><Button variant="ghost" size="sm" onClick={() => markResumeSent(job)}>标记已发送</Button></>} />)}
					{pendingReplies.slice(0, 4).map(item => <div key={`reply-${item.id}`} className="flex flex-wrap items-center gap-3 px-5 py-4"><span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary-soft text-primary"><MessageCircle className="h-4 w-4" /></span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold text-foreground">{item.company || 'HR 消息'}｜{item.title || '待回复'}</span><span className="rounded-full border border-primary-border bg-primary-softer px-2 py-0.5 text-xs text-primary">待回复</span></div><p className="mt-1 truncate text-xs text-text-secondary">{latestHrText(item) || item.detail}</p></div><a href="/monitor" className="inline-flex min-h-9 items-center gap-1 rounded-lg px-3 text-xs font-semibold text-primary hover:bg-primary-soft">处理回复<ChevronRight className="h-3.5 w-3.5" /></a></div>)}
					</div>
					{queueTotal === 0 && <div className="flex flex-wrap items-center gap-3 px-5 py-6"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-primary-soft text-primary"><Check className="h-4 w-4" /></span><div className="min-w-0 flex-1"><h4 className="text-sm font-semibold text-foreground">当前没有待处理事项</h4><p className="mt-0.5 text-xs leading-5 text-text-secondary">新岗位会先进入「待人工确认」，确认后转入「待发送」，届时再点「发送招呼语」。</p></div></div>}
				</div>
			</section>

			<aside id="run-log" className="scroll-mt-4 overflow-hidden rounded-xl border border-card-border bg-surface">
				<div className="border-b border-card-border px-5 py-4"><div className="flex items-center justify-between gap-3"><h3 className="text-base font-semibold text-foreground">运行记录</h3>{activeTask && <Button variant="secondary" size="sm" disabled={Boolean(modePending)} onClick={() => handleModeClick(activeTask.mode as WorkbenchMode)}><Square className="h-3 w-3" />停止</Button>}</div><p className="mt-1 text-xs text-text-tertiary">{activeTask ? `当前阶段：${runLogHeadline}` : '启动任务后，这里会显示实时进度。'}</p></div>
				{visibleTask ? <div className="p-5"><div className={`rounded-lg border p-4 ${taskStatusClass(visibleTask.status)}`}><div className="flex items-center gap-2 text-xs font-semibold text-foreground">{activeTask ? <CircleDot className="h-4 w-4 text-primary status-pulse" /> : <Clock3 className="h-4 w-4 text-text-tertiary" />}{visibleTask.label}</div>{runLogHeadline && <p className="mt-2 text-sm font-semibold text-foreground">{runLogHeadline}</p>}{visibleTask.deadline_at && <p className="mt-1 text-xs text-text-tertiary">预计截止 {new Date(visibleTask.deadline_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</p>}</div><ol className="mt-5 space-y-0">{visibleTask.logs.slice(-8).reverse().map((log, index) => <li key={`${log}-${index}`} className="relative flex gap-3 pb-4 last:pb-0"><span className="relative z-10 mt-1 h-2 w-2 shrink-0 rounded-full bg-primary ring-4 ring-surface" />{index < Math.min(visibleTask.logs.length, 8) - 1 && <span className="absolute left-[3px] top-3 h-full w-px bg-card-border" />}<span className="text-xs leading-5 text-text-secondary">{log}</span></li>)}</ol>{!visibleTask.logs.length && <p className="py-8 text-center text-xs text-text-tertiary">正在等待第一条运行日志…</p>}</div> : <div className="flex flex-col items-center justify-center px-6 py-12 text-center"><ShieldCheck className="h-8 w-8 text-primary" /><h4 className="mt-4 text-sm font-semibold text-foreground">等待启动任务</h4><p className="mt-1 text-xs leading-5 text-text-secondary">浏览器操作、AI 评分和发送结果都会记录在这里。</p></div>}
			</aside>
		</div>
		{selectedJob && <JobDetailModal job={selectedJob} onClose={() => setSelectedJob(null)} />}
	</div>
}

function WorkbenchInput({ label, children }: { label: string; children: ReactNode }) {
	return <label className="block"><span className="mb-2 block text-xs font-semibold text-foreground">{label}</span>{children}</label>
}

/**
 * 流程条上的单个步骤。
 *
 * state 只在任务运行期间才有 active/done 语义；空闲时一律 idle，
 * 卡片只表达"这一步还有多少待办"，不再用历史累计数冒充进度。
 */
function PipelineStepCard({ step, index, state, onNavigate }: {
	step: PipelineStepView
	index: number
	state: 'idle' | 'active' | 'done'
	onNavigate: (anchor: string) => void
}) {
	const actionable = Boolean(step.anchor) && step.count > 0
	const shell = `block w-full rounded-lg border px-3 py-3 text-left transition-colors ${
		state === 'active'
			? 'border-primary-border bg-primary-soft'
			: actionable
				? 'border-card-border bg-surface-raised hover:border-primary-border hover:bg-primary-softer'
				: 'border-card-border bg-surface-raised'
	}`
	const badge = state === 'done'
		? 'bg-primary text-white'
		: state === 'active'
			? 'border border-primary text-primary'
			: 'border border-card-border-strong bg-surface text-text-tertiary'
	const body = (
		<>
			<div className="flex items-center gap-2">
				<span className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-semibold ${badge}`}>
					{state === 'done' ? <Check className="h-3.5 w-3.5" /> : state === 'active' ? <CircleDot className="h-3.5 w-3.5 status-pulse" /> : index + 1}
				</span>
				<span className="truncate text-xs font-semibold text-foreground">{step.label}</span>
			</div>
			<div className="mt-2 flex items-baseline justify-between gap-2 text-xs">
				<span className="text-text-tertiary">{step.countLabel}</span>
				<span className={`tabular-nums font-semibold ${step.count > 0 ? 'text-foreground' : 'text-text-tertiary'}`}>{step.count}</span>
			</div>
			{step.manual && step.count > 0 && <p className="mt-1 text-xs font-medium text-warning">需你确认</p>}
		</>
	)
	if (!actionable) return <li className={shell}>{body}</li>
	if (step.anchor!.startsWith('/')) return <li><a href={step.anchor} className={shell}>{body}</a></li>
	return <li><button type="button" onClick={() => onNavigate(step.anchor!)} className={shell}>{body}</button></li>
}

function QueueGroupHeader({ id, title, description, count, actions }: { id?: string; title: string; description: string; count: number; actions?: ReactNode }) {
	// id 供流程条点击后滚到对应分组；scroll-mt 避免贴住视口顶部。
	return <div id={id} className="flex scroll-mt-4 flex-wrap items-center justify-between gap-3 border-y border-card-border bg-surface-raised px-5 py-2.5 first:border-t-0"><div className="flex min-w-0 items-baseline gap-2"><h4 className="text-xs font-semibold text-foreground">{title}</h4><span className="rounded-full border border-card-border-strong bg-surface px-2 py-0.5 text-xs tabular-nums text-text-secondary">{count}</span><p className="hidden truncate text-xs text-text-tertiary sm:block">{description}</p></div>{actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}</div>
}

function QueueRow({ tone, label, job, detail, selected = false, onToggle, actions }: { tone: 'primary' | 'warning' | 'danger' | 'info'; label: string; job: Job; detail: string; selected?: boolean; onToggle?: () => void; actions: ReactNode }) {
	const toneClass = { primary: 'border-primary-border bg-primary-softer text-primary', warning: 'border-warning/30 bg-warning/10 text-warning', danger: 'border-danger/30 bg-danger/10 text-danger', info: 'border-info/30 bg-info/10 text-info' }[tone]
	const dotClass = { primary: 'bg-primary', warning: 'bg-warning', danger: 'bg-danger', info: 'bg-info' }[tone]
	return <div className={`flex flex-wrap items-center gap-3 px-5 py-4 transition-colors ${selected ? 'bg-primary-softer' : 'hover:bg-surface-raised'}`}>{onToggle ? <input type="checkbox" checked={selected} onChange={onToggle} aria-label={`选择 ${job.company} ${job.title}`} className="h-4 w-4 accent-primary" /> : <span className={`h-2 w-2 shrink-0 rounded-full ${dotClass}`} />}<div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="truncate text-sm font-semibold text-foreground">{job.company}｜{job.title}</span><span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${toneClass}`}>{label}</span></div><p className="mt-1 line-clamp-1 text-xs text-text-secondary">{jobSubtitle(job)} · {detail}</p></div><div className="flex shrink-0 flex-wrap gap-1">{actions}</div></div>
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
	companyFilter,
	setCompanyFilter,
	page,
	pageSize,
	setPage,
	setPageSize,
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
	companyFilter: string
	setCompanyFilter: (company: string) => void
	page: number
	pageSize: number
	setPage: (page: number) => void
	setPageSize: (size: number) => void
	activeTask: WorkbenchTask | null
	refresh: () => Promise<void>
	refreshing: boolean
	startTask: (mode: 'score' | 'reenrich') => Promise<WorkbenchTask>
}) {
	const confirm = useConfirm()
	const [notice, setNotice] = useState('')
	const [starting, setStarting] = useState(false)
	const [regeneratingJobId, setRegeneratingJobId] = useState<string | null>(null)
	const [selectedGreetingIds, setSelectedGreetingIds] = useState<Set<string>>(new Set())
	const [companySearch, setCompanySearch] = useState(companyFilter)
	const scoring = activeTask?.mode === 'score'
	const enriching = activeTask?.mode === 'reenrich'
	const totalPages = Math.max(1, Math.ceil(jobsTotal / pageSize))
	const firstRecord = jobsTotal === 0 ? 0 : (page - 1) * pageSize + 1
	const lastRecord = Math.min(page * pageSize, jobsTotal)
	const paginationItems = useMemo<Array<number | string>>(() => {
		if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1)
		const visiblePages = [...new Set([1, totalPages, page - 1, page, page + 1])]
			.filter(item => item >= 1 && item <= totalPages)
			.sort((left, right) => left - right)
		const items: Array<number | string> = []
		visiblePages.forEach((item, index) => {
			if (index > 0 && item - visiblePages[index - 1] > 1) items.push(`ellipsis-${index}`)
			items.push(item)
		})
		return items
	}, [page, totalPages])
	useEffect(() => setCompanySearch(companyFilter), [companyFilter])
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
		const confirmed = await confirm({
			title: `重新生成「${job.company} - ${job.title}」的招呼语`,
			description: '即使之前已联系过，本次仍会先进入待确认。',
			confirmLabel: '重新生成',
			will: ['覆盖现有的招呼语草稿'],
			wont: ['不会自动发送到 BOSS'],
		})
		if (!confirmed) return
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
		const confirmed = await confirm({
			title: `批量重新生成 ${ids.length} 个岗位的招呼语`,
			description: '完成后统一进入工作台待确认。',
			confirmLabel: `重新生成 ${ids.length} 条`,
			will: ['覆盖现有的招呼语草稿'],
			wont: ['不会自动发送到 BOSS'],
		})
		if (!confirmed) return
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
		<div className="mx-auto max-w-[100rem] animate-fade-in">
			<div className="mb-5 flex flex-wrap items-end justify-between gap-4">
				<div>
					<h2 className="text-2xl font-bold tracking-tight text-foreground">岗位池</h2>
					<p className="mt-1.5 text-sm text-text-secondary">共 {jobsTotal} 个岗位，筛选并审阅最值得跟进的机会。</p>
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
				</div>
			</div>
			<ImportJobPanel activeTask={activeTask} refresh={refresh} />
			{notice && <div className="mb-4 rounded-lg border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-primary">{notice}</div>}
			{activeTask && !scoring && (
				<div className="mb-4 rounded-lg border border-card-border bg-surface-raised px-4 py-3 text-xs text-text-tertiary">
					当前正在运行 "{activeTask.label}"，完成后才能启动岗位池操作。
				</div>
			)}
			<div className="sticky top-0 z-20 mb-4 rounded-xl border border-card-border bg-surface p-3 shadow-sm">
				<form
					className="flex flex-wrap items-center gap-2"
					onSubmit={event => {
						event.preventDefault()
						setCompanyFilter(companySearch.trim())
					}}
				>
					<label htmlFor="company-search" className="sr-only">搜索公司</label>
					<div className="relative min-w-[240px] flex-1 sm:max-w-md">
						<Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
						<input
							id="company-search"
							value={companySearch}
							onChange={event => setCompanySearch(event.target.value)}
							placeholder="搜索公司名称，如：腾讯"
							className="h-10 w-full rounded-lg border border-card-border-strong bg-background pl-10 pr-9 text-sm text-foreground outline-none transition focus:border-primary/50 focus:bg-surface focus:ring-2 focus:ring-primary/15"
						/>
						{companySearch && (
							<button
								type="button"
								aria-label="清空公司搜索"
								className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-text-tertiary hover:bg-surface-raised hover:text-foreground"
								onClick={() => {
									setCompanySearch('')
									if (companyFilter) setCompanyFilter('')
								}}
							>
								<X className="h-3.5 w-3.5" />
							</button>
						)}
					</div>
					<Button type="submit" disabled={companySearch.trim() === companyFilter}>
						<Search className="h-3.5 w-3.5" />
						搜索
					</Button>
				</form>
				<div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-card-border pt-3">
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
				<div className="mt-3 flex flex-wrap items-center gap-2">
					<span className="mr-1 text-sm font-medium text-text-secondary">状态</span>
					<button
						type="button"
						onClick={() => setStatusFilter('all')}
						className={`rounded-lg border px-2.5 py-1 text-xs transition-all ${
							statusFilter === 'all'
								? 'border-primary-border bg-primary-soft text-primary'
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
									? 'border-primary-border bg-primary-soft text-primary'
									: 'border-card-border bg-surface text-text-tertiary hover:border-primary/20 hover:text-foreground'
							}`}
						>
							{getStatusLabel(status)} {statusCounts[status]}
						</button>
					))}
				</div>
				<p className="mt-3 text-xs text-text-tertiary">
					{jobs.length} / {jobsTotal} 条记录{companyFilter && ` · 公司包含“${companyFilter}”`}
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
			{jobsTotal > 0 && (
				<nav className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-card-border bg-surface px-3 py-2.5 shadow-sm" aria-label="岗位池分页">
					<div className="flex items-center gap-3 text-xs text-text-secondary">
						<span>显示 {firstRecord}–{lastRecord}，共 {jobsTotal} 条</span>
						<label className="flex items-center gap-1.5">
							<span className="text-text-tertiary">每页</span>
							<select value={pageSize} onChange={event => setPageSize(Number(event.target.value))} className="h-8 rounded-lg border border-card-border-strong bg-surface px-2 text-xs text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15" aria-label="每页显示数量">
								{[20, 50, 100].map(size => <option key={size} value={size}>{size} 条</option>)}
							</select>
						</label>
					</div>
					<div className="flex items-center gap-1">
						<Button variant="ghost" size="sm" className="h-8 px-2" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="上一页"><ChevronLeft className="h-4 w-4" /></Button>
						{paginationItems.map(item => typeof item === 'number'
							? <button key={item} type="button" onClick={() => setPage(item)} aria-label={`第 ${item} 页`} aria-current={item === page ? 'page' : undefined} className={`h-8 min-w-8 rounded-lg px-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 ${item === page ? 'bg-primary text-white' : 'text-text-secondary hover:bg-surface-raised hover:text-foreground'}`}>{item}</button>
							: <span key={item} className="grid h-8 min-w-6 place-items-center text-xs text-text-tertiary" aria-hidden>…</span>)}
						<Button variant="ghost" size="sm" className="h-8 px-2" disabled={page >= totalPages} onClick={() => setPage(page + 1)} aria-label="下一页"><ChevronRight className="h-4 w-4" /></Button>
					</div>
				</nav>
			)}
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
	const confirm = useConfirm()
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
		const confirmed = await confirm({
			title: '放弃这条待回复建议',
			description: '放弃后该条建议不再出现在待处理列表。',
			confirmLabel: '放弃建议',
			cancelLabel: '保留',
			tone: 'danger',
			wont: ['不会向 HR 发送任何回复'],
		})
		if (!confirmed) return
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
								<div className="mt-2 rounded-lg border border-primary/20 bg-primary/10 px-2 py-1 text-center font-medium text-primary text-xs">{getActionLabel(item.action)}</div>
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
