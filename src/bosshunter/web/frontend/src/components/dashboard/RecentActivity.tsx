import { Badge } from '@/components/ui/badge'
import type { HistoryItem } from '@/hooks/useDashboard'
import { formatLocalDateTime } from '@/lib/utils'

interface RecentActivityProps {
	data: HistoryItem[]
}

const ACTION_LABELS: Record<string, string> = {
	scrape: '采集',
	scored: '评分',
	sent: '发送',
	replied: '回复',
	error: '错误',
	approved: '确认',
	filtered: '过滤',
	resume_sent: '简历',
}

export function RecentActivity({ data }: RecentActivityProps) {
	if (!data.length) {
		return (
			<div className="rounded-lg border border-card-border bg-surface p-4">
				<div className="text-xs text-text-tertiary mb-2">最近活动</div>
				<p className="text-xs text-text-tertiary">暂无活动记录</p>
			</div>
		)
	}

	return (
		<div className="rounded-lg border border-card-border bg-surface p-4">
			<div className="text-xs text-text-tertiary mb-3">最近活动</div>
			<div className="space-y-3">
				{data.map((item, i) => (
					<div key={i} className="flex items-start gap-3">
						<div className="w-1.5 h-1.5 rounded-full bg-text-tertiary mt-2 shrink-0" />
						<div className="flex-1 min-w-0">
							<div className="flex items-center gap-2">
								<Badge variant={item.action as any} className="text-[10px] px-1.5 py-0">
									{ACTION_LABELS[item.action] || item.action}
								</Badge>
								<span className="text-xs text-text-secondary truncate">
									{item.company} · {item.title}
								</span>
							</div>
							<p className="text-[11px] text-text-tertiary mt-0.5">{formatLocalDateTime(item.created_at)}</p>
						</div>
					</div>
				))}
			</div>
		</div>
	)
}
