import { useLocation } from 'react-router-dom'
import { Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/useTheme'

const pageTitles: Record<string, { en: string; cn: string }> = {
	'/': { en: 'Workbench', cn: '工作台' },
	'/jobs': { en: 'Jobs Pool', cn: '岗位池' },
	'/monitor': { en: 'Monitor', cn: '监测执行' },
	'/assistant': { en: 'Career Agent', cn: 'AI 求职助手' },
	'/config': { en: 'Settings', cn: '配置' },
}

export function Header() {
	const location = useLocation()
	const page = pageTitles[location.pathname] || { en: 'BossHunter', cn: 'BossHunter' }
	const { theme, toggleTheme } = useTheme()

	return (
		<header className="h-14 border-b border-card-border bg-surface flex items-center justify-between px-6">
			<div className="flex items-baseline gap-2.5">
				<h1 className="text-[15px] font-semibold text-foreground">{page.cn}</h1>
				<span className="text-xs text-text-tertiary">{page.en}</span>
			</div>
			<div className="flex items-center gap-3 text-xs text-text-secondary">
				<Button
					variant="ghost"
					size="sm"
					className="h-8 w-8 p-0"
					onClick={toggleTheme}
					aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
					title={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
				>
					{theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
				</Button>
				<div className="flex items-center gap-2">
					<span className="h-1.5 w-1.5 rounded-full bg-success status-pulse" aria-hidden />
					<span>在线</span>
				</div>
			</div>
		</header>
	)
}
