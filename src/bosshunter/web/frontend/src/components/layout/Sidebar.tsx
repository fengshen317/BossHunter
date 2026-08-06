import { NavLink } from 'react-router-dom'
import { BriefcaseBusiness, Github, LayoutDashboard, Radar, Settings, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'

const navItems = [
	{ to: '/', icon: LayoutDashboard, label: '工作台' },
	{ to: '/jobs', icon: BriefcaseBusiness, label: '岗位池' },
	{ to: '/monitor', icon: Radar, label: '监测执行' },
	{ to: '/assistant', icon: Sparkles, label: 'AI 助手' },
	{ to: '/config', icon: Settings, label: '配置' },
]

const GITHUB_URL = 'https://github.com/powerycy/BossHunter'

interface SidebarProps {
	pendingReplies?: number
}

export function Sidebar({ pendingReplies: pendingRepliesProp }: SidebarProps) {
	const [pendingReplies, setPendingReplies] = useState(pendingRepliesProp ?? 0)

	useEffect(() => {
		if (pendingRepliesProp !== undefined) {
			setPendingReplies(pendingRepliesProp)
			return
		}

		const fetchPendingReplies = async () => {
			try {
				const res = await fetch('/api/history/unresolved-replies/count')
				const data = await res.json()
				setPendingReplies(Number(data.count) || 0)
			} catch {
				setPendingReplies(0)
			}
		}

		fetchPendingReplies()
		const interval = setInterval(fetchPendingReplies, 30000)
		return () => clearInterval(interval)
	}, [pendingRepliesProp])

	return (
		<aside className="w-60 border-r border-card-border bg-surface/50 flex flex-col">
			<div className="h-16 flex items-center px-5">
				<div className="flex items-center gap-3">
					<div className="w-9 h-9 rounded-lg bg-primary/15 border border-primary/20 flex items-center justify-center">
						<span className="font-logo text-sm text-primary font-bold">BH</span>
					</div>
					<div>
						<div className="text-sm font-semibold text-foreground tracking-tight">BossHunter</div>
						<div className="text-[11px] text-text-tertiary">v2.0 · 本地运行</div>
					</div>
				</div>
			</div>

			<nav className="flex-1 px-3 py-2 space-y-1">
				{navItems.map(item => (
					<NavLink
						key={item.to}
						to={item.to}
						className={({ isActive }) =>
							`flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${
								isActive
									? 'bg-primary/10 text-primary font-medium'
									: 'text-muted hover:text-foreground hover:bg-surface'
							}`
						}
					>
						{({ isActive }) => (
							<>
								{isActive && (
									<div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 bg-primary rounded-r-full" />
								)}
								<span className="flex items-center gap-3">
									<item.icon className={`w-[18px] h-[18px] ${isActive ? 'text-primary' : 'text-text-tertiary'}`} />
									<span>{item.label}</span>
								</span>
								{item.to === '/monitor' && pendingReplies > 0 && (
									<span className="h-2 w-2 rounded-full bg-danger status-pulse" aria-label="有待处理事项" />
								)}
							</>
						)}
					</NavLink>
				))}
			</nav>

			<div className="px-3 py-4 border-t border-card-border">
				<a
					href={GITHUB_URL}
					target="_blank"
					rel="noreferrer"
					className="flex items-center rounded-lg border border-card-border bg-surface px-3 py-2.5 text-xs text-muted transition-all hover:border-primary/20 hover:text-primary hover:bg-surface-raised"
				>
					<Github className="mr-3 h-4 w-4" />
					<span className="flex items-center gap-2">
						<span className="text-yellow-400 text-[11px]">★</span>
						<span className="text-foreground">BossHunter</span>
					</span>
				</a>
			</div>
		</aside>
	)
}
