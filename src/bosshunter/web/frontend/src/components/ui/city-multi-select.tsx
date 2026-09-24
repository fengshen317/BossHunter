import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, ChevronDown, MapPin, Search, X } from 'lucide-react'

interface CityMultiSelectProps {
  value: string[]
  onChange: (value: string[]) => void
  disabled?: boolean
  placeholder?: string
}

const FALLBACK_CITIES = ['北京', '上海', '深圳', '广州', '杭州', '成都', '武汉', '南京']

export function CityMultiSelect({
  value,
  onChange,
  disabled = false,
  placeholder = '选择目标城市',
}: CityMultiSelectProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [cities, setCities] = useState<string[]>([])
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    let cancelled = false
    void fetch('/api/config/cities')
      .then(response => response.ok ? response.json() : Promise.reject(new Error('城市列表加载失败')))
      .then((payload: Record<string, string>) => {
        if (!cancelled) setCities(Object.keys(payload))
      })
      .catch(() => {
        if (!cancelled) setLoadError(true)
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open])

  useEffect(() => {
    if (open) searchRef.current?.focus()
  }, [open])

  const availableCities = cities.length ? cities : Array.from(new Set([...FALLBACK_CITIES, ...value]))
  const filteredCities = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    if (!normalizedQuery) return availableCities
    return availableCities.filter(city => city.toLowerCase().includes(normalizedQuery))
  }, [availableCities, query])

  const toggleCity = (city: string) => {
    onChange(value.includes(city) ? value.filter(item => item !== city) : [...value, city])
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(current => !current)}
        className="flex min-h-10 w-full items-center gap-2 rounded-lg border border-card-border-strong bg-background px-3 py-2 text-left text-sm text-foreground outline-none transition-colors hover:border-primary/40 focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <MapPin className="h-4 w-4 shrink-0 text-text-tertiary" />
        <span className={`min-w-0 flex-1 truncate ${value.length ? 'text-foreground' : 'text-text-tertiary'}`}>
          {value.length ? `已选择 ${value.length} 个城市` : placeholder}
        </span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-text-tertiary transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {value.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {value.map(city => (
            <span key={city} className="inline-flex items-center gap-1 rounded-md border border-primary/20 bg-primary/[0.07] px-2 py-1 text-xs font-medium text-primary">
              {city}
              <button
                type="button"
                disabled={disabled}
                aria-label={`移除${city}`}
                onClick={() => onChange(value.filter(item => item !== city))}
                className="rounded-sm text-primary/70 transition hover:bg-primary/10 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/30"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange([])}
            className="px-1.5 py-1 text-xs text-text-tertiary transition hover:text-danger disabled:cursor-not-allowed disabled:opacity-60"
          >
            清空
          </button>
        </div>
      )}

      {open && !disabled && (
        <div className="absolute z-50 mt-2 w-full min-w-[18rem] overflow-hidden rounded-xl border border-card-border-strong bg-surface shadow-xl shadow-black/10">
          <div className="border-b border-card-border p-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
              <input
                ref={searchRef}
                value={query}
                onChange={event => setQuery(event.target.value)}
                placeholder="搜索城市"
                aria-label="搜索城市"
                className="h-9 w-full rounded-lg border border-card-border bg-background pl-9 pr-3 text-sm text-foreground outline-none placeholder:text-text-tertiary focus:border-primary focus:ring-2 focus:ring-primary/15"
              />
            </div>
          </div>
          <div role="listbox" aria-multiselectable="true" className="max-h-64 overflow-y-auto p-1.5">
            {filteredCities.map(city => {
              const selected = value.includes(city)
              return (
                <button
                  key={city}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => toggleCity(city)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${selected ? 'bg-primary/[0.08] text-primary' : 'text-foreground hover:bg-surface-raised'}`}
                >
                  <span className={`grid h-4 w-4 shrink-0 place-items-center rounded border ${selected ? 'border-primary bg-primary text-white' : 'border-card-border-strong bg-background'}`}>
                    {selected && <Check className="h-3 w-3" />}
                  </span>
                  <span>{city}</span>
                </button>
              )
            })}
            {filteredCities.length === 0 && (
              <p className="px-3 py-6 text-center text-xs text-text-tertiary">没有匹配的城市</p>
            )}
          </div>
          <div className="flex items-center justify-between border-t border-card-border bg-surface-raised px-3 py-2 text-xs text-text-tertiary">
            <span>{loadError ? '完整城市列表加载失败，当前显示常用城市' : `共 ${availableCities.length} 个城市`}</span>
            <button type="button" onClick={() => setOpen(false)} className="font-medium text-primary hover:text-primary/80">完成</button>
          </div>
        </div>
      )}
    </div>
  )
}
