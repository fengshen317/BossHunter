import { useLocation } from 'react-router-dom'
import { Moon, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/hooks/useTheme'
import { useBackendHealth } from '@/hooks/useBackendHealth'

const pageTitles: Record<string, string> = {
	'/': '工作台',
	'/jobs': '岗位池',
	'/monitor': '监测执行',
	'/assistant': 'AI 求职助手',
	'/config': '配置',
}

export function Header() {
	const location = useLocation()
	const page = pageTitles[location.pathname] || 'BossHunter'
	const { theme, toggleTheme } = useTheme()
	const health = useBackendHealth()

	return (
		<header className="flex h-14 shrink-0 items-center justify-end border-b border-card-border bg-surface px-4 sm:px-6">
			<h1 className="mr-auto text-base font-semibold text-foreground lg:hidden">{page}</h1>
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
				<div className="flex items-center gap-2" title={health.online ? '本地后端已连接' : '无法连接本地后端，请确认服务是否在运行'}>
					<span className={`h-1.5 w-1.5 rounded-full ${health.online ? 'bg-success status-pulse' : 'bg-danger'}`} aria-hidden />
					<span>{health.online ? '后端在线' : '后端离线'}</span>
				</div>
			</div>
		</header>
	)
}
