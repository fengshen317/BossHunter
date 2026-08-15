/** @type {import('tailwindcss').Config} */
export default {
	content: [
		"./index.html",
		"./src/**/*.{js,ts,jsx,tsx}",
	],
	theme: {
		extend: {
			colors: {
				/* 克制亮色工作台：近白底 + 单一 teal 强调 + 高对比墨色文字。
				   所有正文/占位符对白底均满足 WCAG AA 4.5:1。 */
				background: 'rgb(var(--color-background) / <alpha-value>)',
				foreground: 'rgb(var(--color-foreground) / <alpha-value>)',
				surface: 'rgb(var(--color-surface) / <alpha-value>)',
				'surface-raised': 'rgb(var(--color-surface-raised) / <alpha-value>)',
				'surface-overlay': 'rgb(var(--color-surface-overlay) / <alpha-value>)',
				'card-border': 'rgb(var(--color-card-border) / <alpha-value>)',
				'card-border-strong': 'rgb(var(--color-card-border-strong) / <alpha-value>)',
				primary: 'rgb(var(--color-primary) / <alpha-value>)',
				'primary-dim': 'rgb(var(--color-primary-dim) / <alpha-value>)',
				'primary-bright': 'rgb(var(--color-primary-bright) / <alpha-value>)',
				success: 'rgb(var(--color-success) / <alpha-value>)',
				warning: 'rgb(var(--color-warning) / <alpha-value>)',
				danger: 'rgb(var(--color-danger) / <alpha-value>)',
				info: 'rgb(var(--color-info) / <alpha-value>)',
				muted: 'rgb(var(--color-muted) / <alpha-value>)',
				'text-secondary': 'rgb(var(--color-text-secondary) / <alpha-value>)',
				'text-tertiary': 'rgb(var(--color-text-tertiary) / <alpha-value>)',
			},
			fontFamily: {
				mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
				sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Microsoft YaHei', 'sans-serif'],
			},
			borderRadius: {
				'lg': '8px',
			},
			boxShadow: {
				/* 仅保留克制、功能性的阴影：卡片轻浮 + 弹层 */
				'card': '0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.06)',
				'card-hover': '0 2px 4px rgba(15, 23, 42, 0.06), 0 4px 12px rgba(15, 23, 42, 0.08)',
				'overlay': '0 8px 24px rgba(15, 23, 42, 0.12), 0 2px 6px rgba(15, 23, 42, 0.08)',
			},
			keyframes: {
				'pulse-dot': {
					'0%, 100%': { opacity: '1' },
					'50%': { opacity: '0.45' },
				},
				'fade-in': {
					'0%': { opacity: '0' },
					'100%': { opacity: '1' },
				},
			},
			animation: {
				'pulse-dot': 'pulse-dot 2.4s ease-in-out infinite',
				'fade-in': 'fade-in 0.2s ease-out',
			},
		},
	},
	plugins: [],
}
