/** @type {import('tailwindcss').Config} */
export default {
	content: [
		"./index.html",
		"./src/**/*.{js,ts,jsx,tsx}",
	],
	theme: {
		extend: {
			colors: {
				background: '#0b0f19',
				foreground: '#e8ecf4',
				surface: '#111827',
				'surface-raised': '#1a2332',
				'surface-overlay': '#0d1220',
				'card-border': '#1f2a3d',
				primary: '#10b981',
				'primary-dim': '#059669',
				'primary-bright': '#34d399',
				success: '#34d399',
				warning: '#fbbf24',
				danger: '#f87171',
				info: '#38bdf8',
				muted: '#94a3b8',
				'text-secondary': '#94a3b8',
				'text-tertiary': '#7a8599',
			},
			fontFamily: {
				mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
				sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Microsoft YaHei', 'sans-serif'],
				logo: ['Space Mono', 'monospace'],
			},
			borderRadius: {
				cyber: '6px',
			},
			boxShadow: {
				'glow': '0 0 20px rgba(16, 185, 129, 0.12)',
				'glow-sm': '0 0 10px rgba(16, 185, 129, 0.08)',
				'glow-lg': '0 0 40px rgba(16, 185, 129, 0.15)',
				'glow-danger': '0 0 20px rgba(248, 113, 113, 0.12)',
				'glow-warning': '0 0 20px rgba(251, 191, 36, 0.12)',
				'card': '0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.2)',
				'card-hover': '0 4px 12px rgba(0,0,0,0.3), 0 2px 4px rgba(0,0,0,0.2)',
			},
			keyframes: {
				'glow-pulse': {
					'0%, 100%': { boxShadow: '0 0 20px rgba(16, 185, 129, 0.12)' },
					'50%': { boxShadow: '0 0 30px rgba(16, 185, 129, 0.18)' },
				},
				'pulse-dot': {
					'0%, 100%': { opacity: '1' },
					'50%': { opacity: '0.4' },
				},
				'fade-in': {
					'0%': { opacity: '0', transform: 'translateY(4px)' },
					'100%': { opacity: '1', transform: 'translateY(0)' },
				},
				'shimmer': {
					'0%': { backgroundPosition: '-200% 0' },
					'100%': { backgroundPosition: '200% 0' },
				},
			},
			animation: {
				'glow-pulse': 'glow-pulse 2s ease-in-out infinite',
				'pulse-dot': 'pulse-dot 2s ease-in-out infinite',
				'fade-in': 'fade-in 0.3s ease-out',
				'shimmer': 'shimmer 2s linear infinite',
			},
		},
	},
	plugins: [],
}
