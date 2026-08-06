import type { FunnelData } from '@/hooks/useDashboard'

interface FunnelCardsProps {
	data: FunnelData
}

const funnelSteps = [
	{ key: '采集总数', color: 'text-info' },
	{ key: '初筛通过', color: 'text-primary' },
	{ key: 'AI评分', color: 'text-emerald-400' },
	{ key: '人工确认', color: 'text-warning' },
]

export function FunnelCards({ data }: FunnelCardsProps) {
	return (
		<div className="grid grid-cols-4 gap-3">
			{funnelSteps.map((step, i) => {
				const count = data[step.key] || 0
				const prevCount = i === 0 ? count : (data[funnelSteps[i - 1].key] || 0)
				const rate = prevCount > 0 && i > 0 ? ((count / prevCount) * 100).toFixed(1) + '%' : ''

				return (
					<div key={step.key} className="rounded-lg border border-card-border bg-surface p-4 cyber-card">
							<div className="text-xs text-text-tertiary">{step.key}</div>
							<div className={`mt-2 text-2xl font-bold tabular-nums data-value ${step.color}`}>{count}</div>
							{rate && <p className="text-xs text-text-tertiary mt-1">{rate}</p>}
					</div>
				)
			})}
		</div>
	)
}
