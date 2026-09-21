import { NavLink } from 'react-router-dom'
import { BriefcaseBusiness, LayoutDashboard, Radar, Settings, Sparkles } from 'lucide-react'

const navItems = [
	{ to: '/', icon: LayoutDashboard, label: '工作台' },
	{ to: '/jobs', icon: BriefcaseBusiness, label: '岗位池' },
	{ to: '/monitor', icon: Radar, label: 'HR 消息' },
	{ to: '/assistant', icon: Sparkles, label: 'AI 助手' },
	{ to: '/config', icon: Settings, label: '配置' },
]
export function Sidebar() {
	return (
		<aside className="flex w-16 shrink-0 flex-col border-r border-card-border bg-surface lg:w-56">
			<div className="flex h-[4.5rem] items-center justify-center border-b border-card-border px-3 lg:justify-start lg:px-5">
				<div className="flex items-center gap-3">
					<div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary shadow-sm">
						<span className="text-sm text-white font-bold tracking-tight">BH</span>
					</div>
					<div className="hidden lg:block">
						<div className="text-sm font-bold tracking-tight text-foreground">BossHunter</div>
						<div className="mt-0.5 text-xs text-text-tertiary">本地求职工作台</div>
					</div>
				</div>
			</div>

			<nav className="flex-1 space-y-1 px-2 py-4 lg:px-3">
				{navItems.map(item => (
					<NavLink
						key={item.to}
						to={item.to}
						aria-label={item.label}
						title={item.label}
						className={({ isActive }) =>
							`flex min-h-10 items-center justify-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors lg:justify-between ${
								isActive
									? 'bg-primary-soft text-primary font-medium'
									: 'text-text-secondary hover:text-foreground hover:bg-surface-raised'
							}`
						}
					>
							<span className="flex items-center gap-3">
								<item.icon className="w-[18px] h-[18px]" />
								<span className="hidden lg:inline">{item.label}</span>
						</span>
					</NavLink>
				))}
			</nav>
		</aside>
	)
}
