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
				'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-not-allowed disabled:opacity-40',
				checked ? 'bg-primary/20 border-primary/40' : 'bg-surface border-card-border',
				className
			)}
		>
			<span
				className={cn(
					'pointer-events-none block h-3.5 w-3.5 rounded-full transition-all duration-200',
					checked
						? 'translate-x-4 bg-primary shadow-[0_0_8px_rgba(0,255,200,0.5)]'
						: 'translate-x-0.5 bg-text-tertiary'
				)}
			/>
		</button>
	)
}
