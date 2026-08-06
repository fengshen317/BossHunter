import { useMemo } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import type { ActivityData } from '@/hooks/useDashboard'

interface TrendChartProps {
	data: ActivityData[]
}

export function TrendChart({ data }: TrendChartProps) {
	const chartData = useMemo(() => {
		const dayMap: Record<string, { day: string; send: number; reply: number }> = {}

		for (let i = 6; i >= 0; i--) {
			const d = new Date()
			d.setDate(d.getDate() - i)
			const key = d.toISOString().split('T')[0]
			dayMap[key] = { day: key.slice(5), send: 0, reply: 0 }
		}

		data.forEach(item => {
			const key = item.day
			if (!dayMap[key]) dayMap[key] = { day: key.slice(5), send: 0, reply: 0 }
			if (item.action === 'sent') dayMap[key].send += item.cnt
			else if (item.action === 'replied') dayMap[key].reply += item.cnt
		})

		return Object.values(dayMap).sort((a, b) => a.day.localeCompare(b.day))
	}, [data])

	return (
		<div className="rounded-lg border border-card-border bg-surface p-4">
			<div className="text-xs text-text-tertiary mb-3">7 日趋势</div>
			<div className="h-[200px]">
				<ResponsiveContainer width="100%" height="100%">
					<LineChart data={chartData}>
						<CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
						<XAxis dataKey="day" stroke="#64748b" fontSize={10} tick={{ fontFamily: 'JetBrains Mono' }} />
						<YAxis stroke="#64748b" fontSize={10} tick={{ fontFamily: 'JetBrains Mono' }} />
						<Tooltip
							contentStyle={{ background: '#111827', border: '1px solid #1e293b', borderRadius: '6px', fontFamily: 'JetBrains Mono', fontSize: '12px' }}
							labelStyle={{ color: '#94a3b8' }}
						/>
						<Legend wrapperStyle={{ fontSize: '11px', fontFamily: 'JetBrains Mono' }} />
						<Line type="monotone" dataKey="send" name="发送" stroke="#34d399" strokeWidth={2} dot={false} />
						<Line type="monotone" dataKey="reply" name="回复" stroke="#38bdf8" strokeWidth={2} dot={false} />
					</LineChart>
				</ResponsiveContainer>
			</div>
		</div>
	)
}
