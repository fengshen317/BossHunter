import { cn } from '@/lib/utils'
import { forwardRef, TextareaHTMLAttributes } from 'react'

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
	({ className, ...props }, ref) => (
		<textarea
			className={cn(
				'flex min-h-[92px] w-full rounded-lg border border-card-border-strong bg-surface px-3 py-2 text-sm leading-6 text-foreground placeholder:text-text-tertiary focus:outline-none focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-45 transition-colors',
				className
			)}
			ref={ref}
			{...props}
		/>
	)
)
Textarea.displayName = 'Textarea'
