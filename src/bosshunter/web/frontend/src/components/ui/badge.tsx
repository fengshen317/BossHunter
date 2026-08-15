import { cn } from '@/lib/utils'
import { cva, type VariantProps } from 'class-variance-authority'
import { HTMLAttributes } from 'react'

/* 统一的 6 个语义角色：所有 status 都映射到其中之一，避免彩虹色板。
   文字色均为 700 级，确保在浅色底上满足小号文字 4.5:1 对比。 */
const badgeVariants = cva(
	'inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-medium border',
	{
		variants: {
			variant: {
				/* 中性：默认/待处理/跳过/已过滤 */
				neutral: 'bg-background text-text-secondary border-card-border',
				default: 'bg-background text-text-secondary border-card-border',
				pending: 'bg-background text-text-secondary border-card-border',
				skipped: 'bg-background text-text-tertiary border-card-border',
				filtered: 'bg-background text-text-tertiary border-card-border',
				/* 信息/进行中：已评分/就绪/已跟进 */
				info: 'bg-info/10 text-info border-info/25',
				scored: 'bg-info/10 text-info border-info/25',
				ready: 'bg-info/10 text-info border-info/25',
				follow_up_sent: 'bg-info/10 text-info border-info/25',
				/* 成功：已发送/已回复/自动回复/已确认 */
				success: 'bg-success/10 text-success border-success/25',
				sent: 'bg-success/10 text-success border-success/25',
				replied: 'bg-success/10 text-success border-success/25',
				auto_replied: 'bg-success/10 text-success border-success/25',
				approved: 'bg-success/10 text-success border-success/25',
				/* 提醒/等待：需简历/待回复 */
				warning: 'bg-warning/10 text-warning border-warning/25',
				needs_resume: 'bg-warning/10 text-warning border-warning/25',
				reply_pending: 'bg-warning/10 text-warning border-warning/25',
				/* 负面：拒绝/错误 */
				danger: 'bg-danger/10 text-danger border-danger/25',
				rejected: 'bg-danger/10 text-danger border-danger/25',
				error: 'bg-danger/10 text-danger border-danger/25',
				closed: 'bg-background text-text-tertiary border-card-border-strong',
				/* 特殊：简历已发 */
				purple: 'bg-[rgba(124,58,237,0.10)] text-[#6D28D9] border-[rgba(124,58,237,0.22)]',
				resume_sent: 'bg-[rgba(124,58,237,0.10)] text-[#6D28D9] border-[rgba(124,58,237,0.22)]',
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
