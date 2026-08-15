import type { TopCompany } from '@/hooks/useDashboard'

interface TopCompaniesProps {
	data: TopCompany[]
}

export function TopCompanies({ data }: TopCompaniesProps) {
	if (!data.length) {
		return (
			<div className="rounded-lg border border-card-border bg-surface p-4">
				<div className="text-xs text-text-tertiary mb-2">热门公司</div>
				<p className="text-xs text-text-tertiary">暂无数据</p>
			</div>
		)
	}

	const maxScore = Math.max(...data.map(d => d.avg_score))

	return (
		<div className="rounded-lg border border-card-border bg-surface p-4">
			<div className="text-xs text-text-tertiary mb-3">热门公司</div>
			<div className="space-y-3">
				{data.map((company, i) => (
					<div key={i} className="flex items-center gap-3">
						<span className="text-[11px] text-text-tertiary w-3">{i + 1}</span>
						<div className="flex-1">
							<div className="flex items-center justify-between mb-1">
								<span className="text-xs text-foreground truncate max-w-[140px]">{company.company}</span>
								<div className="flex items-center gap-2">
									<span className="text-[11px] text-text-tertiary">{company.job_count}岗</span>
									<span className="text-xs text-info data-value">{company.avg_score}</span>
								</div>
							</div>
							<div className="h-1 bg-card-border rounded-full overflow-hidden">
								<div
									className="h-full bg-primary rounded-full transition-all"
									style={{ width: `${(company.avg_score / maxScore) * 100}%` }}
								/>
							</div>
						</div>
					</div>
				))}
			</div>
		</div>
	)
}
