import { useLocation } from 'react-router-dom'
import { Activity } from 'lucide-react'

const pageTitles: Record<string, { en: string; cn: string }> = {
	'/': { en: 'Workbench', cn: '工作台' },
	'/jobs': { en: 'Jobs Pool', cn: '岗位池' },
	'/monitor': { en: 'Monitor', cn: '监测执行' },
	'/assistant': { en: 'AI Assistant', cn: 'AI 助手' },
	'/config': { en: 'Settings', cn: '配置' },
}

export function Header() {
	const location = useLocation()
	const page = pageTitles[location.pathname] || { en: 'BossHunter', cn: 'BossHunter' }

	return (
		<header className="h-14 border-b border-card-border bg-surface/50 flex items-center justify-between px-6">
			<div className="flex items-center gap-3">
				<h1 className="text-sm font-semibold text-foreground">{page.cn}</h1>
				<span className="text-xs text-text-tertiary">{page.en}</span>
			</div>
			<div className="flex items-center gap-2 text-xs text-text-tertiary">
				<span className="h-1.5 w-1.5 rounded-full bg-success status-pulse" />
				<span>在线</span>
			</div>
		</header>
	)
}
