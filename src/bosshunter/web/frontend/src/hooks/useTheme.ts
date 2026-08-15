import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'bosshunter-theme'

function getInitialTheme(): Theme {
	const savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY)
	if (savedTheme === 'light' || savedTheme === 'dark') return savedTheme
	return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function useTheme() {
	const [theme, setTheme] = useState<Theme>(getInitialTheme)

	useEffect(() => {
		document.documentElement.classList.toggle('dark', theme === 'dark')
		document.documentElement.dataset.theme = theme
		window.localStorage.setItem(THEME_STORAGE_KEY, theme)
	}, [theme])

	return {
		theme,
		toggleTheme: () => setTheme(current => current === 'dark' ? 'light' : 'dark'),
	}
}
