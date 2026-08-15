import { cn } from '@/lib/utils'

interface SwitchProps {
	checked: boolean
	onChange: (checked: boolean) => void
	className?: string
	disabled?: boolean
}

export function Switch({ checked, onChange, className, disabled }: SwitchProps) {
	return (
		<button
			type="button"
			role="switch"
			aria-checked={checked}
			disabled={disabled}
			onClick={() => onChange(!checked)}
			className={cn(
				'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-45',
				checked ? 'bg-primary border-primary' : 'bg-surface-raised border-card-border-strong',
				className
			)}
		>
			<span
				className={cn(
					'pointer-events-none block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200',
					checked ? 'translate-x-4' : 'translate-x-0.5'
				)}
			/>
		</button>
	)
}
