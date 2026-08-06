import { cn } from '@/lib/utils'
import { cva, type VariantProps } from 'class-variance-authority'
import { HTMLAttributes } from 'react'

const badgeVariants = cva(
	'inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-medium border',
	{
		variants: {
			variant: {
				default: 'bg-surface-raised text-text-tertiary border-card-border',
				pending: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20',
				scored: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
				ready: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
				approved: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
				skipped: 'bg-zinc-500/10 text-zinc-500 border-zinc-500/20',
				sent: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
				replied: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
				resume_sent: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
				needs_resume: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
				follow_up_sent: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
				reply_pending: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
				auto_replied: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
				rejected: 'bg-red-500/10 text-red-400 border-red-500/20',
				error: 'bg-red-500/10 text-red-400 border-red-500/20',
				filtered: 'bg-zinc-500/10 text-zinc-500 border-zinc-500/20',
			},
		},
		defaultVariants: {
			variant: 'default',
		},
	}
)

export interface BadgeProps extends HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
	return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}
