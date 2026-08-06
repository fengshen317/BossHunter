import { useEffect, useState } from 'react'
import { Check, ClipboardCopy, Loader2, MessageSquareText, RefreshCw, Send, Sparkles, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'

/**
 * AI 助手：把 HR 消息 / 岗位描述粘贴进来，生成回复语或招呼语。
 *
 * 生成结果不写库、不发送——仅供复制到 BOSS 使用，全程人工确认。
 * 后端：
 * - POST /api/replies/generate  (HR 消息 → 回复)
 * - POST /api/greetings/generate (岗位描述 → 招呼语)
 */

interface JobOption {
  id: string
  title: string
  company: string
}

interface GenerateResult {
  message: string
}

/** 粘贴生成结果的通用卡片：结果文本 + 复制按钮。 */
function ResultBlock({
  text,
  onReset,
  copied,
  onCopy,
}: {
  text: string
  onReset: () => void
  copied: boolean
  onCopy: () => void
}) {
  return (
    <div className="space-y-2">
      <div className="greeting-block whitespace-pre-wrap text-foreground">{text}</div>
      <div className="flex gap-2">
        <Button size="sm" variant="secondary" onClick={onCopy}>
          {copied ? <Check className="mr-1.5 h-3.5 w-3.5 text-success" /> : <ClipboardCopy className="mr-1.5 h-3.5 w-3.5" />}
          {copied ? '已复制' : '复制'}
        </Button>
        <Button size="sm" variant="ghost" onClick={onReset}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
          继续生成
        </Button>
      </div>
    </div>
  )
}

function useCopy() {
  const [copied, setCopied] = useState(false)
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // 非安全上下文（如内嵌网页）剪贴板 API 不可用，回退到隐藏 textarea 复制。
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }
  return { copied, copy }
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 text-xs text-danger">{message}</div>
  )
}

export default function AIAssistantPage() {
  const [jobs, setJobs] = useState<JobOption[]>([])

  useEffect(() => {
    fetch('/api/jobs?limit=100')
      .then(res => res.json())
      .then(data => {
        const items: JobOption[] = (data.items || []).map((job: { id: string; title: string; company: string }) => ({
          id: job.id,
          title: job.title,
          company: job.company,
        }))
        setJobs(items)
      })
      .catch(() => setJobs([]))
  }, [])

  return (
    <div className="max-w-4xl space-y-4">
      <GreetingCard jobs={jobs} />
      <ReplyCard jobs={jobs} />
    </div>
  )
}

// ─── 生成招呼语 ─────────────────────────────────────────────

function GreetingCard({ jobs }: { jobs: JobOption[] }) {
  const [title, setTitle] = useState('')
  const [company, setCompany] = useState('')
  const [salary, setSalary] = useState('')
  const [city, setCity] = useState('')
  const [jd, setJd] = useState('')
  const [review, setReview] = useState(false)
  const [jobId, setJobId] = useState('')

  const [notice, setNotice] = useState('')
  const [generating, setGenerating] = useState(false)
  const [result, setResult] = useState<GenerateResult | null>(null)
  const { copied, copy } = useCopy()

  const handleSelectJob = (id: string) => {
    setJobId(id)
    if (!id) return
    const job = jobs.find(j => j.id === id)
    if (!job) return
    setTitle(job.title)
    setCompany(job.company)
    setJd('')
  }

  const handleGenerate = async () => {
    if (!jd.trim() || generating) return
    setGenerating(true)
    setNotice('')
    try {
      const res = await fetch('/api/greetings/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title.trim(),
          company: company.trim(),
          salary: salary.trim(),
          city: city.trim(),
          jd: jd.trim(),
          review,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data.error || '生成失败')
      }
      setResult(data as GenerateResult)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '生成失败，请稍后重试')
    } finally {
      setGenerating(false)
    }
  }

  const handleReset = () => {
    setResult(null)
    setNotice('')
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center gap-2">
        <Wand2 className="h-4 w-4 text-primary" />
        <CardTitle className="text-sm text-foreground">生成招呼语</CardTitle>
        <span className="text-[11px] text-text-tertiary">粘贴岗位描述（JD），AI 基于当前简历生成个性化招呼语</span>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 从岗位池选取，快速带出标题/公司 */}
        <div className="flex items-center gap-2">
          <Select
            value={jobId}
            onChange={e => handleSelectJob(e.target.value)}
            disabled={generating}
            className="flex-1"
          >
            <option value="">从岗位池选择（可选，自动填充）</option>
            {jobs.map(job => (
              <option key={job.id} value={job.id}>
                {job.company}｜{job.title}
              </option>
            ))}
          </Select>
          <span className="text-[11px] text-text-tertiary whitespace-nowrap">或手动填写</span>
        </div>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Input value={title} onChange={e => setTitle(e.target.value)} placeholder="岗位名称（可选）" disabled={generating} />
          <Input value={company} onChange={e => setCompany(e.target.value)} placeholder="公司（可选）" disabled={generating} />
          <Input value={salary} onChange={e => setSalary(e.target.value)} placeholder="薪资（可选）" disabled={generating} />
          <Input value={city} onChange={e => setCity(e.target.value)} placeholder="城市（可选）" disabled={generating} />
        </div>

        <Textarea
          value={jd}
          onChange={e => setJd(e.target.value)}
          placeholder="粘贴岗位描述（JD）原文，必填，如：负责 XX 系统的架构设计与落地，要求 3 年以上 Python 经验，熟悉 LangGraph..."
          disabled={generating}
          className="min-h-[160px]"
        />

        <div className="flex items-center justify-between">
          <label className="flex items-center gap-2 text-xs text-muted">
            <Switch checked={review} onChange={setReview} disabled={generating} />
            启用 AI 自检优化（多轮打磨，质量更高，耗时更久）
          </label>
          <Button onClick={handleGenerate} disabled={generating || !jd.trim()}>
            {generating ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Sparkles className="mr-1.5 h-3.5 w-3.5" />}
            {generating ? '生成中...' : '生成招呼语'}
          </Button>
        </div>

        {notice && <ErrorNotice message={notice} />}
        {result && <ResultBlock text={result.message} onReset={handleReset} copied={copied} onCopy={() => copy(result.message)} />}
      </CardContent>
    </Card>
  )
}

// ─── 生成回复 ───────────────────────────────────────────────

function ReplyCard({ jobs }: { jobs: JobOption[] }) {
  const [hrMessage, setHrMessage] = useState('')
  const [conversationHistory, setConversationHistory] = useState('')
  const [jobId, setJobId] = useState('')

  const [notice, setNotice] = useState('')
  const [generating, setGenerating] = useState(false)
  const [result, setResult] = useState<GenerateResult | null>(null)
  const { copied, copy } = useCopy()

  const handleGenerate = async () => {
    if (!hrMessage.trim() || generating) return
    setGenerating(true)
    setNotice('')
    try {
      const res = await fetch('/api/replies/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          hr_message: hrMessage.trim(),
          conversation_history: conversationHistory.trim(),
          job_id: jobId || undefined,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data.error || '生成失败')
      }
      setResult(data as GenerateResult)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '生成失败，请稍后重试')
    } finally {
      setGenerating(false)
    }
  }

  const handleReset = () => {
    setResult(null)
    setNotice('')
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center gap-2">
        <MessageSquareText className="h-4 w-4 text-primary" />
        <CardTitle className="text-sm text-foreground">生成回复</CardTitle>
        <span className="text-[11px] text-text-tertiary">粘贴 HR 最新消息，AI 基于当前简历生成回复语（监测漏掉 HR 回复时使用）</span>
      </CardHeader>
      <CardContent className="space-y-3">
        <Textarea
          value={hrMessage}
          onChange={e => setHrMessage(e.target.value)}
          placeholder="粘贴 HR 的最新消息，必填，如：方便发一份你的简历吗？"
          disabled={generating}
          className="min-h-[120px]"
        />

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Textarea
            value={conversationHistory}
            onChange={e => setConversationHistory(e.target.value)}
            placeholder="之前的对话记录（可选，帮助 AI 理解上下文）"
            disabled={generating}
            className="min-h-[120px]"
          />
          <div className="space-y-2">
            <Select value={jobId} onChange={e => setJobId(e.target.value)} disabled={generating} className="w-full">
              <option value="">关联岗位（可选，让回复更贴合）</option>
              {jobs.map(job => (
                <option key={job.id} value={job.id}>
                  {job.company}｜{job.title}
                </option>
              ))}
            </Select>
            <p className="text-[11px] leading-5 text-text-tertiary">
              生成结果不会自动发送，请复制到 BOSS 手动发送，保留人工确认。
            </p>
          </div>
        </div>

        <div className="flex items-center justify-end">
          <Button onClick={handleGenerate} disabled={generating || !hrMessage.trim()}>
            {generating ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Send className="mr-1.5 h-3.5 w-3.5" />}
            {generating ? '生成中...' : '生成回复'}
          </Button>
        </div>

        {notice && <ErrorNotice message={notice} />}
        {result && <ResultBlock text={result.message} onReset={handleReset} copied={copied} onCopy={() => copy(result.message)} />}
      </CardContent>
    </Card>
  )
}
