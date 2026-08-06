import { Fragment, useEffect, useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { getStatusLabel } from '@/lib/status'
import { formatTimeAgo } from '@/lib/utils'
import type { Job } from '@/hooks/useDashboard'

interface JobsTableProps {
	jobs: Job[]
}

function statusVariant(status: string) {
	const variants = new Set([
		'pending',
		'scored',
		'filtered',
		'ready',
		'approved',
		'skipped',
		'sent',
		'replied',
		'resume_sent',
		'needs_resume',
		'follow_up_sent',
		'rejected',
		'error',
	])
	return variants.has(status) ? status : 'default'
}

/** 解析注册资本字符串为"万"单位数值（"1亿"→10000），无法解析返回 null。 */
function parseCapitalWan(text?: string): number | null {
	if (!text) return null
	const match = text.match(/(\d+(?:\.\d+)?)\s*(亿|万)/)
	if (!match) return null
	const n = parseFloat(match[1])
	return match[2] === '亿' ? n * 10000 : n
}

/** 注册资本三档样式：≥500万 高亮（资本雄厚）、<50万 标红（不靠谱）、缺失灰色。 */
function capitalClass(text?: string): string {
	const wan = parseCapitalWan(text)
	if (wan === null) return 'text-text-tertiary'
	if (wan >= 500) return 'text-primary font-semibold'
	if (wan < 50) return 'text-danger'
	return 'text-foreground'
}

export function JobsTable({ jobs }: JobsTableProps) {
	const [expanded, setExpanded] = useState<string | null>(null)
	// 直接渲染服务端返回的当前页岗位（分页/筛选已由后端处理）。

	// 轮询刷新（2s）会传入新的 jobs 引用，即使内容没变。只在岗位集合真正变化
	// （job id 序列不同）时才折叠展开的详情，避免轮询打断查看。
	const prevJobIds = useRef('')
	useEffect(() => {
		const ids = jobs.map(j => j.id).join('')
		if (ids !== prevJobIds.current) {
			prevJobIds.current = ids
			setExpanded(null)
		}
	}, [jobs])

	const getScoreColor = (score: number) => {
		if (score >= 80) return 'text-primary'
		if (score >= 60) return 'text-info'
		return 'text-text-tertiary'
	}

	return (
		<div className="rounded-lg border border-card-border bg-[#111827] overflow-hidden">
			<div className="overflow-x-auto">
				<table className="w-full text-sm">
					<thead>
						<tr className="border-b border-card-border bg-[#1a2332] text-xs text-text-tertiary">
							<th className="px-4 py-3 text-left font-medium">公司</th>
							<th className="px-4 py-3 text-left font-medium">职位</th>
							<th className="px-4 py-3 text-left font-medium">区域</th>
							<th className="px-4 py-3 text-left font-medium">企业背景</th>
							<th className="px-4 py-3 text-left font-medium">注册资本</th>
							<th className="px-4 py-3 text-left font-medium">薪资</th>
							<th className="px-4 py-3 text-left font-medium">评分</th>
							<th className="px-4 py-3 text-left font-medium">状态</th>
							<th className="px-4 py-3 text-left font-medium">时间</th>
						</tr>
					</thead>
						<tbody>
							{jobs.length === 0 && (
								<tr>
									<td colSpan={9} className="px-4 py-12 text-center text-xs text-text-tertiary">
										暂无记录
									</td>
								</tr>
							)}
							{jobs.map(job => {
							const isExpanded = expanded === job.id
							const isManualChat = job.id.startsWith('manual_')
							return (
								<Fragment key={job.id}>
									<tr
										className="cursor-pointer border-b border-card-border bg-[#111827] transition-all hover:bg-[#1a2332]"
										onClick={() => setExpanded(isExpanded ? null : job.id)}
									>
										<td className="px-4 py-3">
											<div className="flex items-center gap-2">
												<span className="max-w-[160px] truncate font-medium text-foreground">{job.company}</span>
												{job.company_size && (
													<span className="rounded-md bg-[#1a2332] border border-card-border px-1.5 py-0.5 text-[10px] text-text-tertiary">{job.company_size}</span>
												)}
											</div>
										</td>
										<td className="max-w-[220px] px-4 py-3">
											<div className="flex items-center gap-2 font-medium text-foreground">
												<span className="truncate">{job.title}</span>
												{isManualChat && (
													<span className="shrink-0 rounded-md border border-info/30 bg-info/10 px-1.5 py-0.5 text-[10px] text-info">HR沟通</span>
												)}
											</div>
										</td>
										<td className="px-4 py-3">
											<div className="whitespace-nowrap text-foreground text-xs">{job.district || '—'}</div>
											<div className="mt-0.5 text-[11px] text-text-tertiary">{job.city || '—'}</div>
										</td>
										<td className="min-w-[200px] px-4 py-3">
											<div className="line-clamp-2 text-xs leading-5 text-text-secondary">
												{job.company_background || job.company_industry || '—'}
											</div>
										</td>
										<td className="whitespace-nowrap px-4 py-3 text-xs">
											<span className={capitalClass(job.registered_capital)}>{job.registered_capital || '—'}</span>
										</td>
										<td className="px-4 py-3 text-xs text-text-tertiary">{job.salary || '—'}</td>
										<td className="px-4 py-3">
											<span className={`font-bold data-value ${getScoreColor(job.score)}`}>{job.score || '—'}</span>
										</td>
										<td className="px-4 py-3">
											<Badge variant={statusVariant(job.status) as any}>{getStatusLabel(job.status)}</Badge>
										</td>
										<td className="px-4 py-3">
											<div className="flex items-center gap-2 text-[11px] text-text-tertiary">
												{formatTimeAgo(job.created_at)}
												{isExpanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
											</div>
										</td>
									</tr>
									{isExpanded && (
										<tr className="border-b border-card-border bg-[#1a2332]">
											<td colSpan={9} className="px-6 py-4">
												<div className="grid grid-cols-1 gap-3 text-sm lg:grid-cols-3">
													<div className="rounded-lg border border-card-border bg-[#111827] p-4">
														<p className="mb-2 text-xs text-text-tertiary">职位描述</p>
														<p className="line-clamp-6 leading-6 text-text-secondary text-xs">{job.jd || '无'}</p>
													</div>
													<div className="rounded-lg border border-card-border bg-[#111827] p-4">
														<p className="mb-2 text-xs text-text-tertiary">招呼语</p>
														<div className="line-clamp-6 whitespace-pre-wrap leading-6 text-text-secondary text-xs greeting-block">{job.greeting || '未生成'}</div>
													</div>
													<div className="rounded-lg border border-card-border bg-[#111827] p-4">
														<p className="mb-2 text-xs text-text-tertiary">评分原因</p>
														<p className="line-clamp-6 whitespace-pre-wrap leading-6 text-text-secondary text-xs">{job.score_reason || '无'}</p>
													</div>
													<div className="rounded-lg border border-card-border bg-[#111827] p-4">
														<p className="mb-2 text-xs text-text-tertiary">公司信息</p>
														<p className="leading-6 text-text-secondary text-xs">区域：{job.city || '—'} {job.district || ''}</p>
														<p className="leading-6 text-text-secondary text-xs">背景：{job.company_background || job.company_industry || '—'}</p>
														<p className="leading-6 text-text-secondary text-xs">注册资本：{job.registered_capital || '—'}</p>
													</div>
												</div>
											</td>
										</tr>
									)}
								</Fragment>
							)
						})}
						</tbody>
					</table>
				</div>
			</div>
		)
	}
