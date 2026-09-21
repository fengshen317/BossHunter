import { useCallback, useEffect, useState } from 'react'

export interface BackendHealth {
	online: boolean
	version?: string
	checkedAt: number
}

const HEALTH_INTERVAL_MS = 30000

/**
 * 后端存活探测。
 *
 * 状态指示必须来自真实请求：写死的「已连接 / 在线」绿点会在后端已经挂掉时
 * 继续显示正常，比没有指示更糟。
 */
export function useBackendHealth(intervalMs: number = HEALTH_INTERVAL_MS): BackendHealth {
	const [health, setHealth] = useState<BackendHealth>({ online: false, checkedAt: 0 })

	const check = useCallback(async () => {
		try {
			const response = await fetch('/api/health')
			const data = await response.json().catch(() => ({}))
			setHealth({ online: response.ok && data.status === 'ok', version: data.version, checkedAt: Date.now() })
		} catch {
			setHealth(previous => ({ ...previous, online: false, checkedAt: Date.now() }))
		}
	}, [])

	useEffect(() => {
		void check()
		const timer = window.setInterval(() => { void check() }, intervalMs)
		return () => window.clearInterval(timer)
	}, [check, intervalMs])

	return health
}
