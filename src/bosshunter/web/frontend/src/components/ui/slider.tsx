import { cn } from '@/lib/utils'

interface SliderProps {
	value: number
	onChange: (value: number) => void
	min?: number
	max?: number
	step?: number
	className?: string
}

export function Slider({ value, onChange, min = 0, max = 100, step = 1, className }: SliderProps) {
	const pct = ((value - min) / (max - min)) * 100

	return (
		<div className={cn('relative flex items-center', className)}>
			<input
				type="range"
				min={min}
				max={max}
				step={step}
				value={value}
				onChange={e => onChange(Number(e.target.value))}
				className="w-full h-1.5 rounded-full appearance-none cursor-pointer bg-card-border [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary [&::-webkit-slider-thumb]:cursor-pointer [&::-webkit-slider-thumb]:shadow-[0_0_8px_rgba(0,255,200,0.5)] [&::-webkit-slider-thumb]:border-0"
				style={{
					background: `linear-gradient(to right, #00ffc8 0%, #00c49a ${pct}%, #1e293b ${pct}%, #1e293b 100%)`
				}}
			/>
			<span className="ml-3 text-sm font-mono font-semibold text-primary min-w-[3ch] text-right data-value">{value}</span>
		</div>
	)
}
