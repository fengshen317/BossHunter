import { useState } from 'react'
import { ChevronDown, ChevronUp, Link2, Search, RefreshCw, CheckCircle2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { WorkbenchTask } from '@/hooks/useDashboard'

/**
 * 定向采集岗位：支持「按 URL」和「按公司+职位」两种输入。
 *
 * 后端走 /api/jobs/import（mode=url/search）和 /api/jobs/import/select，
 * 启动的是一个 mode=import 的短任务，会被 useDashboard 的 2s 自动轮询捕获，
 * 所以这里的进度展示直接读父组件透传的 activeTask（无需自建轮询）。
 *
 * 流程：采集 → 入库 → 生成招呼语 → 停在 ready 状态，
 * 用户回到工作台「今日待确定」区人工确认后发送，全程保留强制人工确认。
 */

type ImportMode = 'url' | 'search'

interface ImportCandidate {
	id: string
	title: string
	company: string
	salary: string
	experience?: string
	location?: string
	url: string
}

interface ImportJobPanelProps {
	/** 当前后台任务（可能是 import 任务，也可能是其它任务；用于禁用入口 + 展示进度）。 */
	activeTask: WorkbenchTask | null
	/** 数据刷新回调，启动任务后调用一次以加速状态可见。 */
	refresh: () => Promise<void>
}

export function ImportJobPanel({ activeTask, refresh }: ImportJobPanelProps) {
	const [expanded, setExpanded] = useState(false)
	const [tab, setTab] = useState<ImportMode>('url')

	// URL 模式表单
	const [url, setUrl] = useState('')

	// 搜索模式表单
	const [company, setCompany] = useState('')
	const [title, setTitle] = useState('')

	const [notice, setNotice] = useState('')
	const [submitting, setSubmitting] = useState(false)
	const [candidates, setCandidates] = useState<ImportCandidate[]>([])
	const [selectingId, setSelectingId] = useState<string>('')

	// 是否是本面板触发的 import 任务（用于进度展示和锁定入口）。
	const importTask = activeTask?.mode === 'import' ? activeTask : null
	const busy = Boolean(activeTask) // 任意任务在跑都禁用，避免与长任务争抢浏览器
	const importCompleted = importTask?.status === 'completed'
	const importFailed = importTask?.status === 'failed'

	const resetForm = () => {
		setUrl('')
		setCompany('')
		setTitle('')
		setCandidates([])
		setNotice('')
	}

	const handleTabSwitch = (next: ImportMode) => {
		setTab(next)
		setNotice('')
		setCandidates([])
	}

	const startImportTask = async (body: Record<string, unknown>, endpoint = '/api/jobs/import') => {
		const res = await fetch(endpoint, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		})
		const data = await res.json().catch(() => ({}))
		if (!res.ok) {
			throw new Error(data.error || (data.messages && data.messages.join('；')) || '启动失败')
		}
		// 后台任务已注册到全局 task_runner，useDashboard 的 2s 轮询会自动捕获并更新 activeTask。
		await refresh()
		return data as WorkbenchTask
	}

	const handleSubmitUrl = async () => {
		if (!url.trim() || busy || submitting) return
		setSubmitting(true)
		setNotice('正在采集岗位并生成招呼语...')
		setCandidates([])
		try {
			await startImportTask({ mode: 'url', url: url.trim() })
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '采集启动失败')
		} finally {
			setSubmitting(false)
		}
	}

	const handleSubmitSearch = async () => {
		if (busy || submitting) return
		if (!company.trim() && !title.trim()) {
			setNotice('请至少填写公司名或职位名')
			return
		}
		setSubmitting(true)
		setNotice('正在搜索匹配岗位...')
		setCandidates([])
		try {
			const task = await startImportTask({
				mode: 'search',
				company: company.trim(),
				title: title.trim(),
			})
			// search 模式任务通常很快结束，结束后拉取候选列表。
			await pollForCandidates(task.id)
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '搜索启动失败')
		} finally {
			setSubmitting(false)
		}
	}

	const pollForCandidates = async (taskId: string, maxAttempts = 20) => {
		for (let i = 0; i < maxAttempts; i++) {
			await new Promise(resolve => setTimeout(resolve, 500))
			try {
				const res = await fetch(`/api/jobs/import/${taskId}/candidates`)
				if (!res.ok) continue
				const data = await res.json().catch(() => ({}))
				const list: ImportCandidate[] = data.candidates || []
				// 任务结束后才会有候选（无论空或非空）；通过任务状态判断更稳。
				const taskRes = await fetch('/api/workbench')
				const taskData = await taskRes.json().catch(() => ({}))
				const active = taskData?.active
				const importActive = active && active.mode === 'import' && active.id === taskId
				if (!importActive || active.status === 'completed' || active.status === 'failed') {
					setCandidates(list)
					if (list.length === 0) {
						setNotice('未找到匹配岗位，请调整公司名或职位名后重试')
					} else {
						setNotice(`找到 ${list.length} 个候选岗位，请在下方选择目标`)
					}
					return
				}
			} catch {
				// 网络抖动忽略，继续轮询
			}
		}
		setNotice('搜索超时，请稍后重试')
	}

	const handleSelectCandidate = async (candidate: ImportCandidate) => {
		if (busy || submitting || selectingId) return
		setSelectingId(candidate.id)
		setNotice(`正在采集「${candidate.company} - ${candidate.title}」并生成招呼语...`)
		try {
			await startImportTask({ url: candidate.url }, '/api/jobs/import/select')
			setCandidates([])
		} catch (err) {
			setNotice(err instanceof Error ? err.message : '采集所选岗位失败')
		} finally {
			setSelectingId('')
		}
	}

	const taskStage = (() => {
		if (!importTask) return ''
		const logs = importTask.logs || []
		if (logs.length === 0) return '等待启动...'
		return logs[logs.length - 1]
	})()

	return (
		<section className="mb-4 rounded-lg border border-card-border bg-surface-raised cyber-card">
			<button
				type="button"
				onClick={() => setExpanded(!expanded)}
				className="flex w-full items-center justify-between p-4 text-left"
			>
				<div className="flex items-center gap-2">
					<span className="inline-block w-1.5 h-1.5 rounded-full bg-primary" />
					<span className="text-sm font-semibold text-foreground">定向采集岗位</span>
					<span className="text-[11px] text-text-tertiary">手机端看到心仪岗位，粘贴 URL 或按公司+职位精准导入</span>
				</div>
				{expanded
					? <ChevronUp className="h-4 w-4 text-text-tertiary" />
					: <ChevronDown className="h-4 w-4 text-text-tertiary" />}
			</button>

			{expanded && (
				<div className="border-t border-card-border p-4 space-y-3">
					{/* Tab 切换（手写，无 Tabs 组件） */}
					<div className="flex gap-1 rounded-lg border border-card-border bg-surface p-1">
						<button
							type="button"
							onClick={() => handleTabSwitch('url')}
							className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-all ${
								tab === 'url'
									? 'bg-primary/10 text-primary font-medium'
									: 'text-muted hover:text-foreground'
							}`}
						>
							<Link2 className="h-3.5 w-3.5" />
							按 URL
						</button>
						<button
							type="button"
							onClick={() => handleTabSwitch('search')}
							className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-all ${
								tab === 'search'
									? 'bg-primary/10 text-primary font-medium'
									: 'text-muted hover:text-foreground'
							}`}
						>
							<Search className="h-3.5 w-3.5" />
							按公司+职位
						</button>
					</div>

					{/* notice 提示条 */}
					{notice && (
						<div className={`rounded-lg border px-4 py-3 text-xs ${
							importFailed
								? 'border-danger/30 bg-danger/5 text-danger'
								: 'border-primary/20 bg-primary/5 text-primary'
						}`}>
							{notice}
						</div>
					)}

					{/* import 任务进度（复用 activeTask.logs 末行） */}
					{importTask && (
						<div className="rounded-lg border border-card-border bg-surface px-4 py-3 text-xs text-text-tertiary flex items-center gap-2">
							{importTask.status === 'running' || importTask.status === 'stopping'
								? <RefreshCw className="h-3.5 w-3.5 animate-spin text-primary" />
								: importCompleted
									? <CheckCircle2 className="h-3.5 w-3.5 text-success" />
									: null}
							<span className="text-foreground">{taskStage}</span>
						</div>
					)}

					{/* URL 模式表单 */}
					{tab === 'url' && (
						<div className="flex flex-wrap gap-2">
							<Input
								value={url}
								onChange={e => setUrl(e.target.value)}
								onKeyDown={e => { if (e.key === 'Enter') handleSubmitUrl() }}
								placeholder="粘贴 BOSS 岗位详情页 URL，如 https://www.zhipin.com/job_detail/xxx.html"
								disabled={busy || submitting}
								className="flex-1 min-w-[280px]"
							/>
							<Button onClick={handleSubmitUrl} disabled={busy || submitting || !url.trim()}>
								{submitting
									? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
									: <Link2 className="mr-1.5 h-3.5 w-3.5" />}
								采集并生成招呼语
							</Button>
						</div>
					)}

					{/* 搜索模式表单 */}
					{tab === 'search' && (
						<>
							<div className="flex flex-wrap gap-2">
								<Input
									value={company}
									onChange={e => setCompany(e.target.value)}
									placeholder="公司名（如：字节跳动）"
									disabled={busy || submitting}
									className="flex-1 min-w-[180px]"
								/>
								<Input
									value={title}
									onChange={e => setTitle(e.target.value)}
									onKeyDown={e => { if (e.key === 'Enter') handleSubmitSearch() }}
									placeholder="职位名（如：Python 后端）"
									disabled={busy || submitting}
									className="flex-1 min-w-[180px]"
								/>
								<Button onClick={handleSubmitSearch} disabled={busy || submitting}>
									{submitting
										? <RefreshCw className="mr-1.5 h-3.5 w-3.5 animate-spin" />
										: <Search className="mr-1.5 h-3.5 w-3.5" />}
									搜索
								</Button>
							</div>

							{/* 候选列表 */}
							{candidates.length > 0 && (
								<div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
									{candidates.map(c => (
										<button
											key={c.id}
											type="button"
											onClick={() => handleSelectCandidate(c)}
											disabled={busy || Boolean(selectingId)}
											className={`rounded-lg border p-4 text-left transition-all disabled:opacity-60 ${
												selectingId === c.id
													? 'border-primary/40 bg-primary/5 shadow-glow-sm'
													: 'border-card-border bg-surface hover:border-primary/30'
											}`}
										>
											<div className="font-semibold text-foreground text-sm">
												{c.company}｜{c.title}
											</div>
											<div className="mt-1 text-xs text-text-tertiary">
												{[c.salary, c.experience, c.location].filter(Boolean).join(' · ')}
											</div>
											{selectingId === c.id && (
												<div className="mt-2 text-xs text-primary flex items-center gap-1">
													<RefreshCw className="h-3 w-3 animate-spin" />
													采集中...
												</div>
											)}
										</button>
									))}
								</div>
							)}
						</>
					)}

					{importCompleted && (
						<div className="rounded-lg border border-success/30 bg-success/5 px-4 py-2 text-xs text-success">
							✓ 完成：招呼语已生成，请到工作台「今日待确定」查看并手动确认发送
						</div>
					)}

					<p className="text-[11px] text-text-tertiary">
						提示：定向采集会跳过 AI 评分和关键词过滤（你已手动选定）。岗位入库后状态为「待确认」，仍需你确认后才会发送。
					</p>
				</div>
			)}
		</section>
	)
}
