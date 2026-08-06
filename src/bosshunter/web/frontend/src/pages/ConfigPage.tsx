import { useConfig } from '@/hooks/useConfig'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Slider } from '@/components/ui/slider'
import { TagsInput } from '@/components/ui/tags-input'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Save, RotateCcw, Upload, Trash2, ChevronDown, ChevronRight, Clock3, Plus, RefreshCw } from 'lucide-react'
import { useState, useEffect } from 'react'

const CITIES = [
	'北京', '上海', '深圳', '广州', '杭州', '成都', '武汉', '南京',
	'西安', '苏州', '天津', '重庆', '郑州', '长沙', '东莞', '佛山',
	'合肥', '厦门', '青岛', '大连'
]

export default function ConfigPage() {
	const { config, schema, loading, saving, dirty, error, message, updateConfig, saveConfig, resetConfig } = useConfig()
	const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({ profile: true, search: true })
	const [resumeInfo, setResumeInfo] = useState<any>(null)
	const [avatarInfo, setAvatarInfo] = useState<any>(null)

	useEffect(() => {
		fetch('/api/resume').then(r => r.json()).then(setResumeInfo).catch(() => {})
		fetch('/api/avatar').then(r => r.json()).then(setAvatarInfo).catch(() => {})
	}, [])

	const toggleSection = (key: string) => {
		setExpandedSections(prev => ({ ...prev, [key]: !prev[key] }))
	}

	const handleResumeUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
		const file = e.target.files?.[0]
		if (!file) return
		const form = new FormData()
		form.append('file', file)
		const res = await fetch('/api/resume/upload', { method: 'POST', body: form })
		const data = await res.json()
		if (data.success) {
			setResumeInfo({ filename: data.filename, size: data.size, path: data.path })
			updateConfig('profile.resume_path', data.path)
		}
	}

	const handleResumeDelete = async () => {
		await fetch('/api/resume', { method: 'DELETE' })
		setResumeInfo(null)
		updateConfig('profile.resume_path', '')
	}

	const handleAvatarUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
		const file = e.target.files?.[0]
		if (!file) return
		const form = new FormData()
		form.append('file', file)
		const res = await fetch('/api/avatar/upload', { method: 'POST', body: form })
		const data = await res.json()
		if (data.success) {
			setAvatarInfo({ filename: data.filename, size: data.size, path: data.path })
			updateConfig('profile.avatar_path', data.path)
		}
	}

	const handleAvatarDelete = async () => {
		await fetch('/api/avatar', { method: 'DELETE' })
		setAvatarInfo(null)
		updateConfig('profile.avatar_path', '')
	}

	if (loading) {
		return (
		<div className="flex h-full items-center justify-center">
			<RefreshCw className="h-5 w-5 animate-spin text-text-tertiary" />
		</div>
	)
	}

	if (error || !config) {
		return (
			<div className="flex h-full items-center justify-center">
				<div className="max-w-md rounded-lg border border-danger/30 bg-danger/5 p-6 text-center">
					<div className="text-sm font-semibold text-foreground">配置加载失败</div>
					<p className="mt-2 text-xs leading-6 text-text-tertiary">
							请确认后端服务已启动：bosshunter web 或 127.0.0.1:8686
						</p>
						{error && <p className="mt-3 rounded-lg border border-danger/20 bg-danger/10 px-3 py-2 text-xs text-danger">{error}</p>}
					<Button className="mt-4" size="sm" onClick={resetConfig}>重试</Button>
				</div>
			</div>
		)
	}

	return (
		<div className="h-full overflow-y-auto space-y-4 pr-4 animate-fade-in">
			<div className="flex items-center justify-between sticky top-0 bg-background z-10 py-2">
				<div className="flex items-center gap-3">
	{dirty && <span className="text-xs text-warning">● 未保存</span>}
						{message && (
							<span className={`text-xs ${message.type === 'success' ? 'text-success' : 'text-danger'}`}>
								{message.type === 'success' ? '✓ ' : '✗ '}{message.text}
							</span>
					)}
				</div>
				<div className="flex items-center gap-2">
					<Button variant="ghost" size="sm" onClick={resetConfig}><RotateCcw className="w-3 h-3 mr-1" />重置</Button>
					<Button size="sm" onClick={saveConfig} disabled={saving || !dirty}><Save className="w-3 h-3 mr-1" />{saving ? '...' : '保存'}</Button>
				</div>
			</div>

			<SectionCard title="个人信息" sectionKey="profile" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<div>
						<label className="block text-xs text-text-tertiary mb-2">简历文件</label>
						{resumeInfo ? (
							<div className="flex items-center gap-3 rounded-lg border border-card-border bg-surface-raised p-3">
								<span className="text-sm text-foreground">📄 {resumeInfo.filename}</span>
								<span className="text-xs text-text-tertiary">({(resumeInfo.size / 1024).toFixed(1)} KB)</span>
								<button onClick={handleResumeDelete} className="ml-auto text-danger hover:text-danger/80 transition-colors">
									<Trash2 className="w-4 h-4" />
								</button>
							</div>
						) : (
							<label className="flex cursor-pointer flex-col items-center justify-center rounded-lg border border-dashed border-card-border p-6 transition-all hover:border-primary/30 hover:bg-primary/5">
								<Upload className="mb-2 h-6 w-6 text-text-tertiary" />
								<span className="text-xs text-text-tertiary">拖拽或点击上传 (.md)</span>
								<input type="file" accept=".md" onChange={handleResumeUpload} className="hidden" />
							</label>
						)}
					</div>
					<div>
						<label className="block text-xs text-text-tertiary mb-2">简历头像（可选）</label>
						{avatarInfo ? (
							<div className="flex items-center gap-3 rounded-lg border border-card-border bg-surface-raised p-3">
								<div className="flex h-12 w-10 items-center justify-center rounded-lg bg-primary/10 text-lg">👤</div>
								<div>
									<div className="text-sm text-foreground">{avatarInfo.filename}</div>
									<div className="text-xs text-text-tertiary">{(avatarInfo.size / 1024).toFixed(1)} KB · 仅嵌入本地 PDF</div>
								</div>
								<button onClick={handleAvatarDelete} className="ml-auto text-danger hover:text-danger/80 transition-colors">
									<Trash2 className="w-4 h-4" />
								</button>
							</div>
						) : (
							<label className="flex cursor-pointer items-center gap-3 rounded-lg border border-dashed border-card-border p-4 transition-all hover:border-primary/30 hover:bg-primary/5">
								<Upload className="h-5 w-5 text-text-tertiary" />
								<div>
									<div className="text-xs text-foreground">上传职业头像</div>
									<div className="text-xs text-text-tertiary">JPG、PNG 或 WebP，最大 5MB</div>
								</div>
								<input type="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" onChange={handleAvatarUpload} className="hidden" />
							</label>
						)}
					</div>
					<div className="grid grid-cols-2 gap-4">
						<Field label="最低薪资 (K)">
							<ConfigNumberInput value={config.profile?.salary_min} defaultValue={0} onChange={value => updateConfig('profile.salary_min', value)} min={0} max={200} />
						</Field>
						<Field label="最高薪资 (K)">
							<ConfigNumberInput value={config.profile?.salary_max} defaultValue={0} onChange={value => updateConfig('profile.salary_max', value)} min={0} max={200} />
						</Field>
					</div>
					<Field label="排除关键词">
						<TagsInput value={config.profile?.deal_breakers || []} onChange={v => updateConfig('profile.deal_breakers', v)} placeholder="如：外包、996" />
					</Field>
					<div className="flex items-center justify-between">
						<label className="text-xs text-text-tertiary">接受实习/管培岗位</label>
						<Switch checked={config.profile?.allow_internship ?? false} onChange={v => updateConfig('profile.allow_internship', v)} />
					</div>
				</div>
			</SectionCard>

			<SectionCard title="简历排版" sectionKey="resume" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-5">
					<Field label="视觉模板">
						<div className="grid grid-cols-3 gap-3">
							{[
								{ key: 'modern', name: '现代编辑风', note: '清晰、克制，适合互联网岗位' },
								{ key: 'executive', name: '稳重商务', note: '强调履历，适合管理和商务岗位' },
								{ key: 'ats', name: 'ATS 单栏', note: '无头像，适合招聘系统解析' },
							].map(item => {
								const selected = (config.resume?.template || 'modern') === item.key
								return (
									<button
										key={item.key}
										type="button"
										onClick={() => updateConfig('resume.template', item.key)}
										className={`rounded-lg border p-3 text-left transition-all ${selected ? 'border-primary/40 bg-primary/10 shadow-glow-sm' : 'border-card-border bg-surface-raised hover:border-primary/20'}`}
									>
										<div className="mb-3 h-16 overflow-hidden rounded-lg border border-card-border bg-surface p-2">
											<div className={`mb-1 h-2 w-1/2 rounded ${item.key === 'ats' ? 'bg-text-tertiary' : 'bg-primary'}`} />
											<div className="mb-1 h-1 w-full rounded bg-card-border" />
											<div className="mb-2 h-1 w-4/5 rounded bg-card-border" />
											<div className={`h-1 w-1/3 rounded ${item.key === 'ats' ? 'bg-text-tertiary/60' : 'bg-primary/60'}`} />
										</div>
										<div className="text-xs font-semibold text-foreground">{item.name}</div>
										<div className="mt-1 text-[10px] leading-4 text-text-tertiary">{item.note}</div>
									</button>
								)
							})}
						</div>
					</Field>
					<Field label="主题色">
						<div className="flex items-center gap-3">
							<input
								type="color"
								value={config.resume?.theme_color || '#0F766E'}
								onChange={e => updateConfig('resume.theme_color', e.target.value.toUpperCase())}
								className="h-10 w-14 cursor-pointer rounded-lg border border-card-border bg-surface p-1"
							/>
							<Input value={config.resume?.theme_color || '#0F766E'} onChange={e => updateConfig('resume.theme_color', e.target.value)} />
						</div>
					</Field>
					<div className="flex items-center justify-between rounded-lg border border-card-border bg-surface-raised p-4">
						<div>
							<label className="text-sm font-semibold text-foreground">视觉版显示头像</label>
							<p className="mt-1 text-xs text-text-tertiary">头像只参与本地排版，不会作为 AI 输入。</p>
						</div>
						<Switch checked={config.resume?.show_avatar ?? true} onChange={v => updateConfig('resume.show_avatar', v)} />
					</div>
					<div className="flex items-center justify-between rounded-lg border border-card-border bg-surface-raised p-4">
						<div>
							<label className="text-sm font-semibold text-foreground">同时生成 ATS 版本</label>
							<p className="mt-1 text-xs text-text-tertiary">额外生成无头像、单栏 PDF，便于招聘系统解析。</p>
						</div>
						<Switch checked={config.resume?.generate_ats_version ?? true} onChange={v => updateConfig('resume.generate_ats_version', v)} />
					</div>
				</div>
			</SectionCard>

			<SectionCard title="搜索设置" sectionKey="search" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<Field label="搜索关键词">
						<TagsInput value={config.search?.keywords || []} onChange={v => updateConfig('search.keywords', v)} />
					</Field>
					<Field label="城市">
						<div className="flex flex-wrap gap-2">
							{CITIES.map(city => {
								const cities = (config.search?.cities?.length ? config.search.cities : config.profile?.target_cities) || []
								const selected = cities.includes(city)
								return (
									<button
										key={city}
										type="button"
										onClick={() => {
											const newCities = selected ? cities.filter((c: string) => c !== city) : [...cities, city]
											updateConfig('search.cities', newCities)
											updateConfig('profile.target_cities', newCities)
										}}
										className={`px-2.5 py-1 text-xs rounded-lg border transition-all ${selected ? 'border-primary/40 bg-primary/10 text-primary' : 'border-card-border bg-surface-raised text-text-tertiary hover:border-primary/20 hover:text-foreground'}`}
									>
										{city}
									</button>
								)
							})}
						</div>
					</Field>
					<Field label="每关键词翻页数">
						<ConfigNumberInput value={config.search?.max_pages} defaultValue={3} onChange={value => updateConfig('search.max_pages', value)} min={1} max={10} />
					</Field>
				</div>
			</SectionCard>

			<SectionCard title="评分设置" sectionKey="scoring" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<Field label={`通过阈值: ${config.scoring?.threshold || 60}`}>
						<Slider value={config.scoring?.threshold || 60} onChange={v => updateConfig('scoring.threshold', v)} min={0} max={100} />
					</Field>
					<Field label="每轮最大候选数">
						<ConfigNumberInput value={config.scoring?.max_candidates} defaultValue={20} onChange={value => updateConfig('scoring.max_candidates', value)} min={1} max={100} />
					</Field>
					<Field label="AI 评分并发数">
						<ConfigNumberInput value={config.scoring?.concurrency} defaultValue={3} onChange={value => updateConfig('scoring.concurrency', value)} min={1} max={5} />
						<p className="mt-1 text-xs text-text-tertiary">同时进行的深度评分请求数。提高可加快评分，但可能触发模型服务限流。</p>
					</Field>
				</div>
			</SectionCard>

			<SectionCard title="反检测设置" sectionKey="throttle" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<div className="grid grid-cols-3 gap-4">
						<Field label="每日发送上限">
							<ConfigNumberInput value={config.throttle?.daily_limit} defaultValue={30} onChange={value => updateConfig('throttle.daily_limit', value)} />
						</Field>
						<Field label="最短间隔 (秒)">
							<ConfigNumberInput value={config.throttle?.interval_min} defaultValue={60} onChange={value => updateConfig('throttle.interval_min', value)} />
						</Field>
						<Field label="最长间隔 (秒)">
							<ConfigNumberInput value={config.throttle?.interval_max} defaultValue={180} onChange={value => updateConfig('throttle.interval_max', value)} />
						</Field>
					</div>
					<div className="flex items-center justify-between">
						<label className="text-xs text-text-tertiary">发送前模拟浏览</label>
						<Switch checked={config.throttle?.browse_before_greet ?? true} onChange={v => updateConfig('throttle.browse_before_greet', v)} />
					</div>
					<div className="flex items-center justify-between rounded-lg border border-card-border bg-surface-raised p-4">
						<div>
							<label className="text-sm font-semibold text-foreground">仅向在线 HR 发送</label>
							<p className="mt-1 text-xs text-text-tertiary leading-5">发送前先读取岗位页状态；仅 HR 显示"在线"或"刚刚活跃"时发送。</p>
						</div>
						<Switch checked={config.throttle?.only_send_when_hr_online ?? false} onChange={v => updateConfig('throttle.only_send_when_hr_online', v)} />
					</div>
					<div className="grid grid-cols-2 gap-4">
						<Field label="浏览最短时长 (秒)">
							<ConfigNumberInput value={config.throttle?.browse_duration_min} defaultValue={15} onChange={value => updateConfig('throttle.browse_duration_min', value)} />
						</Field>
						<Field label="浏览最长时长 (秒)">
							<ConfigNumberInput value={config.throttle?.browse_duration_max} defaultValue={30} onChange={value => updateConfig('throttle.browse_duration_max', value)} />
						</Field>
					</div>
					<Field label="发送时间窗口">
						<SendWindowPicker
							value={config.throttle?.send_windows || ['09:00-16:00']}
							onChange={value => updateConfig('throttle.send_windows', value)}
						/>
						<p className="mt-1 text-xs text-text-tertiary">后台任务会在当天最后一个发送窗口结束时自动停止。</p>
					</Field>
					<Field label="随机休息概率">
						<ConfigNumberInput value={config.throttle?.day_off_probability} defaultValue={0.05} onChange={value => updateConfig('throttle.day_off_probability', value)} step={0.01} min={0} max={1} />
					</Field>
				</div>
			</SectionCard>

			<SectionCard title="AI 设置" sectionKey="ai" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<Field label="提供商">
						<select
							value={config.ai?.provider || 'anthropic'}
							onChange={e => updateConfig('ai.provider', e.target.value)}
							className="h-10 w-full rounded-lg border border-card-border bg-surface-raised px-3 text-sm text-foreground outline-none focus:border-primary/40 focus:ring-2 focus:ring-primary/15 transition-colors"
						>
							<option value="anthropic">Anthropic Messages</option>
							<option value="openai_compatible">OpenAI 兼容接口（DeepSeek 等）</option>
						</select>
						<p className="mt-1 text-xs text-text-tertiary">
							{config.ai?.provider === 'openai_compatible'
								? '调用 /chat/completions。DeepSeek、OpenAI 兼容网关请选择此项。'
								: '调用 Anthropic Messages API。Claude 或 Anthropic 兼容网关请选择此项。'}
						</p>
					</Field>
					<Field label="模型名称">
						<Input value={config.ai?.model || ''} onChange={e => updateConfig('ai.model', e.target.value)} />
					</Field>
					<Field label="API Key">
						<Input type="password" value={config.ai?.api_key || ''} onChange={e => updateConfig('ai.api_key', e.target.value)} placeholder={config.ai?.api_key_masked || '也可通过环境变量设置'} />
					</Field>
					<Field label="Base URL">
						<Input value={config.ai?.base_url || ''} onChange={e => updateConfig('ai.base_url', e.target.value)} placeholder="留空使用默认" />
					</Field>
				</div>
			</SectionCard>

			<SectionCard title="监控设置" sectionKey="monitor" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<Field label="检查间隔 (分钟)">
						<ConfigNumberInput value={config.monitor?.interval} defaultValue={30} onChange={value => updateConfig('monitor.interval', value)} min={1} max={120} />
					</Field>
					<Field label="聊天页 URL">
						<Input value={config.monitor?.chat_url || ''} onChange={e => updateConfig('monitor.chat_url', e.target.value)} />
					</Field>
					<div className="flex items-center justify-between rounded-lg border border-card-border bg-surface-raised p-4">
						<div>
							<label className="text-sm font-semibold text-foreground">检测到 HR 消息时自动回复</label>
							<p className="mt-1 text-xs text-text-tertiary">默认关闭。关闭时会为所有未回复消息生成建议，需要你在“监测执行”中确认后发送。</p>
						</div>
						<Switch checked={config.monitor?.auto_reply_hr_questions ?? false} onChange={v => updateConfig('monitor.auto_reply_hr_questions', v)} />
					</div>
				</div>
			</SectionCard>

			<SectionCard title="跟进设置" sectionKey="follow_up" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<div className="flex items-center justify-between">
						<label className="text-xs text-text-tertiary">启用自动跟进</label>
						<Switch checked={config.follow_up?.enabled ?? false} onChange={v => updateConfig('follow_up.enabled', v)} />
					</div>
					<Field label="跟进间隔 (小时)">
						<ConfigNumberInput value={config.follow_up?.interval_hours} defaultValue={48} onChange={value => updateConfig('follow_up.interval_hours', value)} min={12} max={168} />
					</Field>
					<div className="flex items-center justify-between">
						<label className="text-xs text-text-tertiary">跳过周末节假日</label>
						<Switch checked={config.follow_up?.skip_weekends ?? true} onChange={v => updateConfig('follow_up.skip_weekends', v)} />
					</div>
				</div>
			</SectionCard>

			<SectionCard title="去重设置" sectionKey="dedup" expanded={expandedSections} toggle={toggleSection}>
				<div className="space-y-4">
					<Field label="历史记录文件路径">
						<Input value={config.dedup?.history_file || ''} onChange={e => updateConfig('dedup.history_file', e.target.value)} />
					</Field>
				</div>
			</SectionCard>
		</div>
	)
}

function SectionCard({ title, sectionKey, expanded, toggle, children }: {
	title: string; sectionKey: string; expanded: Record<string, boolean>; toggle: (k: string) => void; children: React.ReactNode
}) {
	const isExpanded = expanded[sectionKey] ?? false
	return (
		<Card>
			<button
				className="w-full flex items-center justify-between p-4 transition-colors hover:bg-surface-raised"
				onClick={() => toggle(sectionKey)}
			>
				<span className="text-sm font-semibold text-foreground">{title}</span>
				{isExpanded ? <ChevronDown className="w-4 h-4 text-text-tertiary" /> : <ChevronRight className="w-4 h-4 text-text-tertiary" />}
			</button>
			{isExpanded && <div className="px-4 pb-4">{children}</div>}
		</Card>
	)
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
	return (
		<div>
			<label className="block text-xs text-text-tertiary mb-1.5">{label}</label>
			{children}
		</div>
	)
}

interface ConfigNumberInputProps {
	value: number | undefined
	defaultValue: number
	onChange: (value: number) => void
	min?: number
	max?: number
	step?: number
}

function ConfigNumberInput({ value, defaultValue, onChange, min, max, step }: ConfigNumberInputProps) {
	const resolvedValue = value ?? defaultValue
	const [textValue, setTextValue] = useState(String(resolvedValue))

	useEffect(() => {
		setTextValue(String(resolvedValue))
	}, [resolvedValue])

	const commitDefaultIfNeeded = () => {
		if (textValue === '' || !Number.isFinite(Number(textValue))) {
			setTextValue(String(defaultValue))
			onChange(defaultValue)
		}
	}

	return (
		<Input
			type="number"
			value={textValue}
			min={min}
			max={max}
			step={step}
			onChange={event => {
				const nextValue = event.target.value
				setTextValue(nextValue)
				if (nextValue !== '' && Number.isFinite(Number(nextValue))) {
					onChange(Number(nextValue))
				}
			}}
			onBlur={commitDefaultIfNeeded}
		/>
	)
}

interface TimeRange {
	start: string
	end: string
}

function parseTimeRanges(windows: string[]): TimeRange[] {
	return windows.reduce<TimeRange[]>((ranges, window) => {
		const [start, end] = window.split('-')
		if (/^\d{2}:\d{2}$/.test(start) && /^\d{2}:\d{2}$/.test(end)) {
			ranges.push({ start, end })
		}
		return ranges
	}, [])
}

function SendWindowPicker({ value, onChange }: { value: string[]; onChange: (value: string[]) => void }) {
	const ranges = parseTimeRanges(value)
	const updateRange = (index: number, key: keyof TimeRange, nextValue: string) => {
		const next = ranges.map((range, rangeIndex) => (
			rangeIndex === index ? { ...range, [key]: nextValue } : range
		))
		onChange(next.map(range => `${range.start}-${range.end}`))
	}
	const removeRange = (index: number) => onChange(ranges.filter((_, rangeIndex) => rangeIndex !== index).map(
		range => `${range.start}-${range.end}`
	))
	const addRange = () => onChange([...ranges.map(range => `${range.start}-${range.end}`), '09:00-18:00'])

	return (
		<div className="rounded-lg border border-card-border bg-surface-raised p-3">
			<div className="mb-2 flex items-center gap-2 text-xs text-text-tertiary">
				<Clock3 className="h-3.5 w-3.5 text-primary" />
				发送时间窗口
			</div>
			<div className="space-y-2">
				{ranges.map((range, index) => (
					<div key={`${range.start}-${range.end}-${index}`} className="flex items-center gap-2 rounded-lg bg-surface p-2">
						<Input
							aria-label={`第 ${index + 1} 个窗口开始时间`}
							className="h-9 w-[118px] text-sm"
							type="time"
							value={range.start}
							onChange={event => updateRange(index, 'start', event.target.value)}
						/>
						<span className="text-xs text-text-tertiary">—</span>
						<Input
							aria-label={`第 ${index + 1} 个窗口结束时间`}
							className="h-9 w-[118px] text-sm"
							type="time"
							value={range.end}
							onChange={event => updateRange(index, 'end', event.target.value)}
						/>
						<Button
							aria-label={`删除第 ${index + 1} 个发送窗口`}
							className="ml-auto"
							variant="ghost"
							size="icon"
							type="button"
							onClick={() => removeRange(index)}
						>
							<Trash2 className="h-3.5 w-3.5 text-danger" />
						</Button>
					</div>
				))}
			</div>
			<Button className="mt-3" variant="secondary" size="sm" type="button" onClick={addRange}>
				<Plus className="mr-1 h-3.5 w-3.5" />添加时间范围
			</Button>
		</div>
	)
}
