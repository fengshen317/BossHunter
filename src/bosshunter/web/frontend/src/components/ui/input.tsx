import { cn } from '@/lib/utils'
import { forwardRef, InputHTMLAttributes } from 'react'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
	({ className, type, ...props }, ref) => (
		<input
			type={type}
			className={cn(
				'flex h-9 w-full rounded-lg border border-card-border bg-surface px-3 py-1 text-sm text-foreground placeholder:text-text-tertiary focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-40 transition-colors',
				className
			)}
			ref={ref}
			{...props}
		/>
	)
)
Input.displayName = 'Input'
