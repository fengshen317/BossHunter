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
				className="w-full h-1.5 rounded-full appearance-none cursor-pointer bg-card-border-strong [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-white [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-primary [&::-webkit-slider-thumb]:cursor-pointer [&::-webkit-slider-thumb]:shadow-sm"
				style={{
					background: `linear-gradient(to right, #0F766E 0%, #0F766E ${pct}%, #CBD2DC ${pct}%, #CBD2DC 100%)`
				}}
			/>
			<span className="ml-3 text-sm font-mono font-semibold text-primary min-w-[3ch] text-right data-value">{value}</span>
		</div>
	)
}
