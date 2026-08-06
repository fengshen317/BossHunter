import { useState, KeyboardEvent } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

interface TagsInputProps {
	value: string[]
	onChange: (tags: string[]) => void
	placeholder?: string
	className?: string
}

export function TagsInput({ value, onChange, placeholder = '输入后按回车添加', className }: TagsInputProps) {
	const [input, setInput] = useState('')

	const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
		if (e.key === 'Enter' && input.trim()) {
			e.preventDefault()
			if (!value.includes(input.trim())) {
				onChange([...value, input.trim()])
			}
			setInput('')
		} else if (e.key === 'Backspace' && !input && value.length > 0) {
			onChange(value.slice(0, -1))
		}
	}

	const removeTag = (index: number) => {
		onChange(value.filter((_, i) => i !== index))
	}

	return (
		<div className={cn(
			'flex flex-wrap gap-1.5 min-h-[36px] p-2 rounded-lg border border-card-border bg-surface focus-within:border-primary/40 focus-within:ring-2 focus-within:ring-primary/15 transition-colors',
			className
		)}>
			{value.map((tag, i) => (
				<span
					key={i}
					className="inline-flex items-center gap-1 rounded-lg bg-primary/10 border border-primary/20 px-2 py-0.5 text-xs text-primary"
				>
					{tag}
					<button
						type="button"
						onClick={() => removeTag(i)}
						className="text-primary/60 hover:text-primary transition-colors"
					>
						<X className="w-3 h-3" />
					</button>
				</span>
			))}
			<input
				value={input}
				onChange={e => setInput(e.target.value)}
				onKeyDown={handleKeyDown}
				placeholder={value.length === 0 ? placeholder : ''}
				className="flex-1 min-w-[80px] bg-transparent text-sm text-foreground placeholder:text-text-tertiary outline-none"
			/>
		</div>
	)
}
