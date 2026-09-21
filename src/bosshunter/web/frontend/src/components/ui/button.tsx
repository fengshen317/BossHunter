import { cn } from '@/lib/utils'
import { cva, type VariantProps } from 'class-variance-authority'
import { ButtonHTMLAttributes, forwardRef } from 'react'

export const buttonVariants = cva(
	'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 focus-visible:ring-offset-1 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-45',
	{
		variants: {
			variant: {
				default: 'bg-primary text-white hover:bg-primary-dim active:bg-primary-dim shadow-sm',
				secondary: 'border border-card-border-strong bg-surface text-foreground hover:bg-surface-raised hover:border-card-border-strong',
				destructive: 'bg-danger text-white hover:bg-red-700 active:bg-red-700 shadow-sm',
				outline: 'border border-danger/30 bg-danger/5 text-danger hover:bg-danger/10',
				ghost: 'text-text-secondary hover:text-foreground hover:bg-surface-raised',
			},
			size: {
				default: 'h-9 px-4 py-2',
				sm: 'h-8 px-3 text-xs',
				lg: 'h-10 px-6',
				icon: 'h-9 w-9',
			},
		},
		defaultVariants: {
			variant: 'default',
			size: 'default',
		},
	}
)

export interface ButtonProps
	extends ButtonHTMLAttributes<HTMLButtonElement>,
		VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
	({ className, variant, size, ...props }, ref) => (
		<button
			className={cn(buttonVariants({ variant, size, className }))}
			ref={ref}
			{...props}
		/>
	)
)
Button.displayName = 'Button'
