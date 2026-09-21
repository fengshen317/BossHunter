import { useEffect, useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Building2, ExternalLink, MapPin, RefreshCw, Sparkles } from 'lucide-react'
import { getStatusLabel } from '@/lib/status'
import { formatTimeAgo } from '@/lib/utils'
import type { Job } from '@/hooks/useDashboard'

interface JobsTableProps {
	jobs: Job[]
	onRegenerateGreeting?: (job: Job) => void
	regeneratingJobId?: string | null
	actionsDisabled?: boolean
	selectedJobIds?: Set<string>
	onToggleJob?: (jobId: string) => void
	onToggleAll?: (jobIds: string[]) => void
}

function statusVariant(status: string) {
	return new Set(['pending', 'scored', 'filtered', 'ready', 'approved', 'skipped', 'sent', 'replied', 'resume_sent', 'needs_resume', 'follow_up_sent', 'rejected', 'error', 'closed']).has(status) ? status : 'default'
}

/** 只允许打开可信的 HTTP(S) 职位链接，避免脏数据注入可执行协议。 */
function safeJobUrl(url: string): string | null {
	try {
		const parsed = new URL(url)
		return ['http:', 'https:'].includes(parsed.protocol) ? parsed.toString() : null
	} catch {
		return null
	}
}

function scoreTone(score: number) {
	if (score >= 80) return 'text-primary'
	if (score >= 60) return 'text-info'
	return 'text-text-secondary'
}

export function JobsTable({ jobs, onRegenerateGreeting, regeneratingJobId, actionsDisabled = false, selectedJobIds = new Set<string>(), onToggleJob, onToggleAll }: JobsTableProps) {
	const [activeJobId, setActiveJobId] = useState<string | null>(jobs[0]?.id ?? null)
	const selectableJobIds = useMemo(() => jobs.filter(job => !['rejected', 'closed'].includes(job.status) && !job.id.startsWith('manual_')).map(job => job.id), [jobs])
	const activeJob = jobs.find(job => job.id === activeJobId) ?? jobs[0] ?? null
	const allChecked = selectableJobIds.length > 0 && selectableJobIds.every(id => selectedJobIds.has(id))

	useEffect(() => {
		if (!jobs.some(job => job.id === activeJobId)) setActiveJobId(jobs[0]?.id ?? null)
	}, [activeJobId, jobs])

	if (!activeJob) return (
		<div className="flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed border-card-border-strong bg-surface px-6 text-center">
			<Building2 className="h-8 w-8 text-text-tertiary" />
			<h3 className="mt-4 text-base font-semibold text-foreground">没有找到符合条件的岗位</h3>
			<p className="mt-1 max-w-md text-sm leading-6 text-text-secondary">调整搜索词或状态筛选，或者先采集一批新岗位。</p>
		</div>
	)

	const activeUrl = safeJobUrl(activeJob.url)
	return (
		<div className="grid min-h-[36rem] overflow-hidden rounded-xl border border-card-border bg-surface shadow-sm xl:grid-cols-[minmax(22rem,0.82fr)_minmax(34rem,1.18fr)]">
			<section className="min-w-0 border-b border-card-border xl:border-b-0 xl:border-r" aria-label="岗位列表">
				<div className="flex min-h-12 items-center justify-between border-b border-card-border bg-surface px-4">
					<label className="flex items-center gap-2 text-sm font-medium text-foreground">
						<input type="checkbox" aria-label="选择当前页岗位" checked={allChecked} disabled={!selectableJobIds.length || actionsDisabled} onChange={() => onToggleAll?.(selectableJobIds)} />
						当前结果
					</label>
					<span className="text-xs text-text-tertiary">{jobs.length} 条</span>
				</div>
				<div className="max-h-[44rem] overflow-y-auto">
					{jobs.map(job => {
						const isActive = job.id === activeJob.id
						const canSelect = !['rejected', 'closed'].includes(job.status) && !job.id.startsWith('manual_')
						return (
							<div key={job.id} role="button" tabIndex={0} aria-pressed={isActive} className={`relative flex w-full cursor-pointer gap-3 border-b border-card-border px-4 py-3.5 text-left transition-[background-color,box-shadow] duration-150 last:border-b-0 ${isActive ? 'bg-primary-soft ring-1 ring-inset ring-primary-border' : 'hover:bg-surface-raised'}`} onClick={() => setActiveJobId(job.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') setActiveJobId(job.id) }}>
								<input type="checkbox" className="mt-1" aria-label={`选择 ${job.company} - ${job.title}`} checked={selectedJobIds.has(job.id)} disabled={!canSelect || actionsDisabled} onClick={event => event.stopPropagation()} onChange={() => onToggleJob?.(job.id)} />
								<span className="min-w-0 flex-1">
									<span className="flex items-start justify-between gap-3">
										<span className="min-w-0"><span className="block truncate text-sm font-semibold text-foreground">{job.title}</span><span className="mt-1 block truncate text-sm font-medium text-primary">{job.company}</span></span>
										<span className="shrink-0 text-right"><span className="block text-sm font-semibold text-foreground">{job.salary || '薪资面议'}</span><span className={`data-value mt-1 block text-sm font-bold ${scoreTone(job.score)}`}>{job.score || '—'} <span className="font-normal text-text-tertiary">分</span></span></span>
									</span>
									<span className="mt-2 flex flex-wrap items-center gap-2 text-xs text-text-secondary"><span>{[job.city, job.district].filter(Boolean).join(' · ') || '地点未知'}</span><span aria-hidden>·</span><span>{job.company_size || job.company_industry || '企业信息待补全'}</span><Badge variant={statusVariant(job.status) as any} className="ml-auto">{getStatusLabel(job.status)}</Badge></span>
								</span>
							</div>
						)
					})}
				</div>
			</section>

			<section className="min-w-0" aria-label={`${activeJob.company} ${activeJob.title} 详情`}>
				<header className="border-b border-card-border px-5 py-5 lg:px-6">
					<div className="flex flex-wrap items-start justify-between gap-4">
						<div className="min-w-0">
							<div className="flex flex-wrap items-center gap-2"><h3 className="text-xl font-bold tracking-tight text-foreground">{activeJob.title}</h3><Badge variant={statusVariant(activeJob.status) as any}>{getStatusLabel(activeJob.status)}</Badge></div>
							<div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-text-secondary">
								{activeUrl ? <a href={activeUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-semibold text-primary hover:underline">{activeJob.company}<ExternalLink className="h-3.5 w-3.5" /></a> : <span className="font-semibold text-foreground">{activeJob.company}</span>}
								<span className="inline-flex items-center gap-1"><MapPin className="h-3.5 w-3.5" />{[activeJob.city, activeJob.district].filter(Boolean).join(' · ') || '地点未知'}</span><span>{activeJob.experience || '经验不限'}</span><span className="font-semibold text-foreground">{activeJob.salary || '薪资面议'}</span>
							</div>
						</div>
						<div className="min-w-32 rounded-xl border border-primary-border bg-primary-softer px-4 py-3 shadow-sm"><div className="flex items-baseline justify-between gap-3"><span className={`data-value text-2xl font-bold ${scoreTone(activeJob.score)}`}>{activeJob.score || '—'}</span><span className="text-xs font-medium text-primary">匹配度</span></div><div className="mt-2 h-1.5 overflow-hidden rounded-full bg-primary-border"><div className="h-full rounded-full bg-primary transition-[width] duration-200" style={{ width: `${Math.max(0, Math.min(activeJob.score || 0, 100))}%` }} /></div></div>
					</div>
				</header>

				<div className="grid lg:grid-cols-[minmax(0,1fr)_15rem]">
					<div className="space-y-7 p-5 lg:p-6">
						<div className="rounded-xl border border-primary-border bg-primary-softer p-4 shadow-sm"><div className="mb-3 flex items-center gap-2"><span className="grid h-7 w-7 place-items-center rounded-lg bg-primary-soft"><Sparkles className="h-4 w-4 text-primary" /></span><h4 className="text-sm font-semibold text-foreground">AI 匹配分析</h4></div><p className="whitespace-pre-wrap text-sm leading-7 text-text-secondary">{activeJob.score_reason || '该岗位尚未生成评分原因，可先进行 AI 评分。'}</p></div>
						<div><h4 className="mb-3 text-sm font-semibold text-foreground">职位描述</h4><p className="max-h-72 overflow-y-auto whitespace-pre-wrap pr-2 text-sm leading-7 text-text-secondary">{activeJob.jd || '暂无职位描述'}</p></div>
						<div><h4 className="mb-3 text-sm font-semibold text-foreground">招呼语</h4><div className="greeting-block whitespace-pre-wrap">{activeJob.greeting || '尚未生成招呼语'}</div>{onRegenerateGreeting && !['rejected', 'closed'].includes(activeJob.status) && !activeJob.id.startsWith('manual_') && <Button className="mt-3" variant="secondary" size="sm" disabled={actionsDisabled || regeneratingJobId === activeJob.id} onClick={() => onRegenerateGreeting(activeJob)}><RefreshCw className={`h-3.5 w-3.5 ${regeneratingJobId === activeJob.id ? 'animate-spin' : ''}`} />重新生成招呼语</Button>}</div>
					</div>
					<aside className="border-t border-card-border bg-surface-raised p-5 lg:border-l lg:border-t-0">
						<div className="flex items-center gap-2"><span className="grid h-7 w-7 place-items-center rounded-lg border border-card-border bg-surface"><Building2 className="h-4 w-4 text-primary" /></span><h4 className="text-sm font-semibold text-foreground">公司信息</h4></div>
						<dl className="mt-4 divide-y divide-card-border text-sm"><div className="pb-3"><dt className="text-xs text-text-tertiary">规模</dt><dd className="mt-1 font-medium text-foreground">{activeJob.company_size || '待补全'}</dd></div><div className="py-3"><dt className="text-xs text-text-tertiary">行业</dt><dd className="mt-1 font-medium text-foreground">{activeJob.company_industry || '待补全'}</dd></div><div className="py-3"><dt className="text-xs text-text-tertiary">企业背景</dt><dd className="mt-1 leading-6 text-text-secondary">{activeJob.company_background || '待补全'}</dd></div><div className="py-3"><dt className="text-xs text-text-tertiary">注册资本</dt><dd className="mt-1 font-medium text-foreground">{activeJob.registered_capital || '待补全'}</dd></div><div className="pt-3"><dt className="text-xs text-text-tertiary">更新时间</dt><dd className="mt-1 text-text-secondary">{formatTimeAgo(activeJob.updated_at || activeJob.created_at)}</dd></div></dl>
						{activeUrl && <a href={activeUrl} target="_blank" rel="noreferrer" className="mt-6 inline-flex h-9 w-full items-center justify-center gap-2 rounded-lg border border-primary-border bg-surface px-3 text-sm font-semibold text-primary transition hover:bg-primary-soft"><ExternalLink className="h-4 w-4" />在 BOSS 查看职位</a>}
					</aside>
				</div>
			</section>
		</div>
	)
}
