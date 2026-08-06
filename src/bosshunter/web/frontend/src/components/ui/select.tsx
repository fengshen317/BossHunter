import { cn } from '@/lib/utils'
import { forwardRef, SelectHTMLAttributes } from 'react'

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
	({ className, children, ...props }, ref) => (
		<select
			className={cn(
				'flex h-9 w-full rounded-lg border border-card-border bg-surface px-3 py-1 text-sm text-foreground focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-40 appearance-none transition-colors',
				className
			)}
			ref={ref}
			{...props}
		>
			{children}
		</select>
	)
)
Select.displayName = 'Select'
