import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { buttonVariants } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export interface ConfirmOptions {
	/** 标题说清「要做什么」，不要用「确定吗？」这种无信息量的问题。 */
	title: string
	description?: string
	/** 动词 + 对象，例如「删除 3 个对话」。 */
	confirmLabel: string
	cancelLabel?: string
	tone?: 'default' | 'danger'
	/** 明确「将发生」与「不会发生」，让确认变成知情同意而不是点确认过卡。 */
	will?: string[]
	wont?: string[]
}

type ConfirmFn = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

/**
 * 把 window.confirm 换成真正的无障碍对话框。
 *
 * 原生 confirm 的问题：阻塞主线程、无法样式化、无法说明后果、移动端体验差，
 * 而且会让「人工确认」这个核心安全承诺退化成一次无信息量的点击。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
	const [options, setOptions] = useState<ConfirmOptions | null>(null)
	const [open, setOpen] = useState(false)
	const resolverRef = useRef<((value: boolean) => void) | null>(null)

	const confirm = useCallback<ConfirmFn>(next => {
		setOptions(next)
		setOpen(true)
		return new Promise<boolean>(resolve => { resolverRef.current = resolve })
	}, [])

	const settle = useCallback((value: boolean) => {
		const resolve = resolverRef.current
		resolverRef.current = null
		setOpen(false)
		resolve?.(value)
	}, [])

	const value = useMemo(() => confirm, [confirm])

	return (
		<ConfirmContext.Provider value={value}>
			{children}
			<AlertDialog open={open} onOpenChange={next => { if (!next) settle(false) }}>
				<AlertDialogContent>
					<AlertDialogHeader>
						<AlertDialogTitle>{options?.title}</AlertDialogTitle>
						{options?.description && <AlertDialogDescription>{options.description}</AlertDialogDescription>}
					</AlertDialogHeader>
					{(options?.will?.length || options?.wont?.length) ? (
						<div className="space-y-3 text-xs">
							{options?.will?.length ? (
								<div>
									<div className="font-medium text-foreground">将发生</div>
									<ul className="mt-1 list-disc space-y-0.5 pl-5 text-text-secondary">
										{options.will.map(item => <li key={item}>{item}</li>)}
									</ul>
								</div>
							) : null}
							{options?.wont?.length ? (
								<div>
									<div className="font-medium text-foreground">不会发生</div>
									<ul className="mt-1 list-disc space-y-0.5 pl-5 text-text-secondary">
										{options.wont.map(item => <li key={item}>{item}</li>)}
									</ul>
								</div>
							) : null}
						</div>
					) : null}
					<AlertDialogFooter>
						{/* 取消是安全出口，必须用中性外观：本项目的 outline 变体是危险样式。 */}
						<AlertDialogCancel
							className={cn(buttonVariants({ variant: 'secondary' }), 'mt-2 sm:mt-0')}
							onClick={() => settle(false)}
						>
							{options?.cancelLabel || '取消'}
						</AlertDialogCancel>
						<AlertDialogAction
							onClick={() => settle(true)}
							className={cn(options?.tone === 'danger' && 'bg-danger text-white hover:bg-danger/90')}
						>
							{options?.confirmLabel || '确认'}
						</AlertDialogAction>
					</AlertDialogFooter>
				</AlertDialogContent>
			</AlertDialog>
		</ConfirmContext.Provider>
	)
}

export function useConfirm(): ConfirmFn {
	const context = useContext(ConfirmContext)
	if (!context) throw new Error('useConfirm 必须在 ConfirmProvider 内使用')
	return context
}
