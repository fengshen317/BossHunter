import { NavLink } from 'react-router-dom'
import { BriefcaseBusiness, LayoutDashboard, Radar, Settings, Sparkles } from 'lucide-react'

const navItems = [
	{ to: '/', icon: LayoutDashboard, label: '工作台' },
	{ to: '/jobs', icon: BriefcaseBusiness, label: '岗位池' },
	{ to: '/monitor', icon: Radar, label: '监测执行' },
	{ to: '/assistant', icon: Sparkles, label: 'AI 助手' },
	{ to: '/config', icon: Settings, label: '配置' },
]
export function Sidebar() {
	return (
		<aside className="w-60 shrink-0 border-r border-card-border bg-surface flex flex-col">
			<div className="h-16 flex items-center px-5">
				<div className="flex items-center gap-3">
					<div className="w-9 h-9 rounded-lg bg-primary flex items-center justify-center shadow-sm">
						<span className="text-sm text-white font-bold tracking-tight">BH</span>
					</div>
					<div>
						<div className="text-[11px] text-text-tertiary">v2.0 · 本地运行</div>
					</div>
				</div>
			</div>

			<nav className="flex-1 px-3 py-2 space-y-0.5">
				{navItems.map(item => (
					<NavLink
						key={item.to}
						to={item.to}
						className={({ isActive }) =>
							`flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
								isActive
									? 'bg-primary/10 text-primary font-medium'
									: 'text-text-secondary hover:text-foreground hover:bg-surface-raised'
							}`
						}
					>
						<span className="flex items-center gap-3">
							<item.icon className="w-[18px] h-[18px]" />
							<span>{item.label}</span>
						</span>
					</NavLink>
				))}
			</nav>
		</aside>
	)
}
