import { FormEvent, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { Bot, Brain, Check, ChevronDown, ChevronRight, Clipboard, Code2, Database, ExternalLink, Globe2, Loader2, Minus, Pause, Play, Plus, Send, ShieldCheck, Square, ThumbsDown, ThumbsUp, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useBackendHealth } from '@/hooks/useBackendHealth'
import { useConfirm } from '@/components/confirm-dialog'

interface Conversation { id: string; title: string; preview?: string }
interface Job { id: string; title: string; company: string }
interface Citation {
  source: 'database' | 'resume' | 'memory' | 'web' | 'company_research' | 'structured_data' | 'private_document' | 'public_current'
  label: string
  url?: string
  excerpt?: string
}
interface ApprovalItem { id: string; title?: string; company?: string; greeting?: string; message?: string; detail?: string }
interface Approval {
  action_id?: string
  type?: 'collect_jobs' | 'score_jobs' | 'complete_job_details' | 'generate_greetings' | 'send_greetings' | 'monitor_replies' | 'run_pipeline' | 'send_hr_reply' | 'reply_draft_approval' | 'execute_readonly_sql'
  title: string
  summary?: string
  draft?: string
  affected_items?: ApprovalItem[]
	sql?: string
	parameters?: unknown[]
	tables?: string[]
	readonly?: boolean
	row_limit?: number
	timeout_seconds?: number
	sql_hash?: string
  allowed_actions: string[]
}
interface TaskCard { id: string; mode: string; label: string; status: string; logs: string[]; error?: string; created_at: string; updated_at?: string }
type ExecutionStatus = 'pending' | 'running' | 'waiting' | 'awaiting_approval' | 'completed' | 'failed' | 'stopping' | 'stopped' | 'skipped' | 'approval_required' | 'started' | 'incomplete'
interface ToolTrace { name: string; status: ExecutionStatus }
interface PlanStep {
  id: string
  label: string
  status: ExecutionStatus
  capability?: string
  depends_on?: string[]
  requires_approval?: boolean
  arguments?: Record<string, unknown>
}
interface ExecutionEvent {
  version?: number
  id: string
  parent_id?: string
  kind?: 'phase' | 'plan' | 'tool' | 'agent' | 'approval' | 'task' | 'result'
  phase?: 'understanding' | 'planning' | 'retrieval' | 'generation' | 'approval' | 'execution' | 'finalization'
  label: string
  status: ExecutionStatus
  summary?: string
  detail?: string
  started_at?: string
  finished_at?: string
  duration_ms?: number
  capability?: string
  tool_name?: string
  requires_approval?: boolean
}
interface DagStepResult {
  id: string
  status: ExecutionStatus
  result?: unknown
  error?: string | null
  duration_ms: number
}
interface DagResult {
  status: 'completed' | 'failed' | 'cancelled' | 'awaiting_approval' | 'budget_exhausted' | 'invalid'
  steps: Record<string, DagStepResult>
  execution_order: string[][]
  error?: string | null
  pending_approval_step_ids: string[]
  duration_ms: number
}
type EvidenceDomain = 'direct' | 'structured_data' | 'private_document' | 'public_current' | 'mixed'
interface EvidenceRoute { mode: EvidenceDomain; domains: EvidenceDomain[]; reason: string; freshness_required: boolean }
interface EvidenceTask {
  id: string
  question: string
  evidence_type: EvidenceDomain
  depends_on: string[]
  required: boolean
  max_results: number
  timeout_seconds: number
  status: string
}
interface EvidenceItem { claim: string; value: unknown; source_type: EvidenceDomain; source_label: string }
interface EvidenceResult {
  task_id: string
  executor: string
  status: 'completed' | 'partial' | 'insufficient' | 'failed'
  evidence: EvidenceItem[]
  summary: string
  missing_evidence: string[]
}
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  message_type: 'text' | 'reply_draft' | 'approval'
  metadata?: { run_id?: string; citations?: Citation[]; approval?: Approval | null; task?: TaskCard | null; tool_trace?: ToolTrace[]; execution_plan?: PlanStep[]; execution_events?: ExecutionEvent[]; dag_result?: DagResult | null; evidence_route?: EvidenceRoute; evidence_tasks?: EvidenceTask[]; evidence_results?: EvidenceResult[]; evidence_coverage?: { complete?: boolean; missing_task_ids?: string[] }; capabilities?: string[] }
}
interface Memory { id: string; content: string; memory_type: string }
interface MessagePage { items: ChatMessage[]; next_cursor: string | null; has_more: boolean }

const MESSAGE_PAGE_SIZE = 40

const quickPrompts = [
  '采集 Python Agent 岗位，沿用已配置城市',
  '开始为待处理岗位评分',
  '为已评分岗位生成招呼语',
  '分析我当前的投递情况',
]
const citationIcon = { database: Database, resume: Clipboard, memory: Brain, web: Globe2, company_research: Brain, structured_data: Database, private_document: Clipboard, public_current: Globe2 }

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.error || '请求失败')
  return data as T
}

function renderMarkdown(content: string, citations: Citation[] = []): string {
  const withLinks = content.replace(/\[来源\s*(\d+)\]/g, (full, number: string) => {
    const citation = citations[Number(number) - 1]
	if (!citation) return full
	return citation.url
	  ? `<a class="assistant-inline-citation" href="${citation.url}" target="_blank" rel="noopener noreferrer" aria-label="查看来源 ${number}">${number}</a>`
	  : `<span class="assistant-inline-citation" aria-label="来源 ${number}">${number}</span>`
  })
  return DOMPurify.sanitize(marked.parse(withLinks, { breaks: true, gfm: true }) as string)
}

function upsertExecutionEvent(events: ExecutionEvent[] = [], next: ExecutionEvent): ExecutionEvent[] {
  const existingIndex = events.findIndex(event => event.id === next.id)
  if (existingIndex < 0) return [...events, next]
  return events.map((event, index) => index === existingIndex ? next : event)
}

function normalizeExecutionEvents(events: ExecutionEvent[] = []): ExecutionEvent[] {
  return events.reduce<ExecutionEvent[]>((normalized, event) => upsertExecutionEvent(normalized, event), [])
}

function finalizeExecutionEvents(events: ExecutionEvent[] = []): ExecutionEvent[] {
  // 回答结束后仍停留在 running 的步骤从未收到终态事件。如实标记为「未收到终态」，
  // 不再改写成 completed：把没有终态的步骤说成已完成，等于伪造执行结果。
  return events.map(event => ['running', 'started', 'stopping'].includes(event.status) ? { ...event, status: 'incomplete' as ExecutionStatus } : event)
}

function citationLabel(citation: Citation): string {
  return citation.label.replace(/^\[\d+\]\s*/, '').trim() || '未命名来源'
}

/** 审批结果。历史消息没有持久化决定，只能从后续消息推出，必须如实区分四种终态。 */
type ApprovalOutcome = 'pending' | 'approved' | 'rejected' | 'abandoned' | 'edited'

function approvalOutcomeChip(outcome: ApprovalOutcome): { label: string; tone: StatusTone } {
	if (outcome === 'pending') return { label: '等待审批', tone: 'warning' }
	if (outcome === 'approved') return { label: '已确认执行', tone: 'success' }
	if (outcome === 'rejected') return { label: '已取消，未执行', tone: 'danger' }
	if (outcome === 'edited') return { label: '已提交修改，等待再次确认', tone: 'primary' }
	return { label: '未处理，已继续新对话', tone: 'muted' }
}

/** 复用 badge.tsx 既有的 6 个语义角色，不新增颜色。 */
const toneBadgeVariant: Record<StatusTone, 'neutral' | 'info' | 'success' | 'warning' | 'danger'> = {
	success: 'success',
	danger: 'danger',
	warning: 'warning',
	primary: 'info',
	neutral: 'neutral',
	muted: 'neutral',
}

/** 「跳过」「未收到终态」「已取消」不能只靠颜色表达，必须同时给出文字。 */
function statusNote(status: ExecutionStatus | BusinessStageStatus): string {
	if (status === 'skipped') return '已跳过'
	if (status === 'incomplete') return '未收到终态'
	if (status === 'cancelled') return '已取消'
	if (status === 'stopped') return '已停止'
	return ''
}

/** 证据域归属只认后端声明的字段，不用字符串包含猜测。 */
const capabilityDomain: Record<string, EvidenceDomain> = {
	job_query: 'structured_data',
	job_database: 'structured_data',
	resume_read: 'private_document',
	web_search: 'public_current',
	company_analysis: 'public_current',
	company_research: 'public_current',
}

const executorDomain: Record<string, EvidenceDomain> = {
	job_query: 'structured_data',
	job_database: 'structured_data',
	analytics: 'structured_data',
	resume_read: 'private_document',
	private_docs: 'private_document',
	web_search: 'public_current',
	company_research: 'public_current',
	web_research: 'public_current',
}

/** 待确认时必须说清在等什么，不能把只读查询也叫「回复草稿」。 */
const approvalNouns: Record<string, string> = {
	execute_readonly_sql: '只读数据查询',
	collect_jobs: '岗位采集任务',
	score_jobs: '岗位评分任务',
	complete_job_details: '企业信息补全任务',
	generate_greetings: '招呼语生成任务',
	send_greetings: '招呼语发送',
	send_hr_reply: 'HR 回复草稿',
	reply_draft_approval: 'HR 回复草稿',
	monitor_replies: '回复监测任务',
	run_pipeline: '完整求职流程',
}

function approvalNoun(approval?: Approval | null): string {
	if (!approval) return '待确认操作'
	return approvalNouns[approval.type || ''] || '待确认操作'
}

const capabilityRows: Record<string, { label: string; icon: typeof Database }> = {
	job_query: { label: '查询岗位数据', icon: Database },
	job_collect: { label: '采集岗位', icon: Database },
	job_score: { label: '岗位评分', icon: Database },
	resume_read: { label: '读取当前简历', icon: Clipboard },
	company_analysis: { label: '公司公开信息', icon: Globe2 },
	web_search: { label: '联网检索', icon: Globe2 },
	greeting_generate: { label: '生成招呼语', icon: Send },
	pipeline: { label: '完整求职流程', icon: Play },
}

export default function AIAssistantPage() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
	const [messageCursor, setMessageCursor] = useState<string | null>(null)
	const [hasOlderMessages, setHasOlderMessages] = useState(false)
	const [loadingOlderMessages, setLoadingOlderMessages] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobId, setJobId] = useState('')
  const [memories, setMemories] = useState<Memory[]>([])
  // 联网检索固定开启，保证回答能够引用最新公开信息。
  const webEnabled = true
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [paused, setPaused] = useState(false)
  const [interrupted, setInterrupted] = useState(false)
  const [editingDraft, setEditingDraft] = useState('')
  const [resolutionNotice, setResolutionNotice] = useState('')
  const [selectedConversations, setSelectedConversations] = useState<string[]>([])
  const [conversationSelectMode, setConversationSelectMode] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
	const shouldFollowLatestRef = useRef(true)
  const streamAbortRef = useRef<AbortController | null>(null)
  const streamRunIdRef = useRef<string | null>(null)
  const streamedContentRef = useRef('')
	const streamRenderFrameRef = useRef<number | null>(null)

	useEffect(() => () => {
	  streamAbortRef.current?.abort()
	  if (streamRenderFrameRef.current !== null) window.cancelAnimationFrame(streamRenderFrameRef.current)
	}, [])

  const activeApproval = useMemo(
    () => messages[messages.length - 1]?.metadata?.approval || null,
    [messages],
  )
  const activeApprovalMessageId = activeApproval ? messages[messages.length - 1]?.id : null
  // 状态指示必须来自真实探测：写死的绿点会在后端挂掉时继续说「已连接」。
  const health = useBackendHealth()
  const confirm = useConfirm()
  const latestCapabilities = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index]
      if (message.role !== 'assistant') continue
      const declared = message.metadata?.capabilities
      if (declared?.length) return declared
    }
    return []
  }, [messages])
  // 历史审批没有持久化的决定，只能从后续消息推出，绝不能一律显示「已处理」。
  const approvalOutcomeFor = useCallback((index: number): ApprovalOutcome => {
    if (!messages[index]?.metadata?.approval) return 'pending'
    if (index >= messages.length - 1) return 'pending'
    const next = messages[index + 1]
    if (!next) return 'pending'
    if (next.metadata?.approval) return 'edited'
    if (next.role === 'user') return 'abandoned'
    const text = (next.content || '').trim()
    if (text.startsWith('已取消本次任务') || text.includes('不会执行任何操作')) return 'rejected'
    return 'approved'
  }, [messages])
  const loadConversations = useCallback(async () => {
    const data = await readJson<{ items: Conversation[] }>(await fetch('/api/assistant/conversations'))
    setConversations(data.items || [])
    return data.items || []
  }, [])
  const loadMessages = useCallback(async (id: string) => {
	const data = await readJson<MessagePage>(await fetch(`/api/assistant/conversations/${id}/messages?limit=${MESSAGE_PAGE_SIZE}`))
    setMessages(data.items || [])
	setMessageCursor(data.next_cursor || null)
	setHasOlderMessages(Boolean(data.has_more))
	shouldFollowLatestRef.current = true
  }, [])
	const loadOlderMessages = useCallback(async () => {
		if (!conversationId || !messageCursor || !hasOlderMessages || loadingOlderMessages) return
		const container = scrollRef.current
		const previousHeight = container?.scrollHeight || 0
		setLoadingOlderMessages(true)
		try {
			const data = await readJson<MessagePage>(await fetch(
				`/api/assistant/conversations/${conversationId}/messages?limit=${MESSAGE_PAGE_SIZE}&before=${encodeURIComponent(messageCursor)}`,
			))
			setMessages(current => {
				const existingIds = new Set(current.map(message => message.id))
				return [...(data.items || []).filter(message => !existingIds.has(message.id)), ...current]
			})
			setMessageCursor(data.next_cursor || null)
			setHasOlderMessages(Boolean(data.has_more))
			requestAnimationFrame(() => {
				if (container) container.scrollTop += container.scrollHeight - previousHeight
			})
		} catch (err) {
			setError(err instanceof Error ? err.message : '加载更早消息失败')
		} finally {
			setLoadingOlderMessages(false)
		}
	}, [conversationId, hasOlderMessages, loadingOlderMessages, messageCursor])
  const loadMemories = useCallback(async () => {
    const data = await readJson<{ items: Memory[] }>(await fetch('/api/assistant/memories'))
    setMemories(data.items || [])
  }, [])
  const createConversation = useCallback(async () => {
    const item = await readJson<Conversation>(await fetch('/api/assistant/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: '新的求职对话' }),
    }))
    setConversationId(item.id)
    setMessages([])
    setInput('')
    await loadConversations()
    return item.id
  }, [loadConversations])

  useEffect(() => {
    Promise.all([
      loadConversations(),
      loadMemories(),
      fetch('/api/jobs?limit=100').then(response => response.json()).then(data => setJobs((data.items || []).map((job: Job) => ({
        id: job.id, title: job.title, company: job.company,
      })))),
    ]).then(([items]) => {
      if (items.length) {
        setConversationId(items[0].id)
        loadMessages(items[0].id)
      } else {
        createConversation()
      }
    }).catch(err => setError(err instanceof Error ? err.message : '初始化失败'))
  }, [createConversation, loadConversations, loadMemories, loadMessages])

  useEffect(() => {
	if (!shouldFollowLatestRef.current) return
	scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: sending ? 'auto' : 'smooth',
    })
  }, [messages, sending])

  const savePartialAnswer = async (conversationId: string, content: string, runId: string | null) => {
    const trimmed = content.trim()
    if (!trimmed) return false
    try {
      await readJson(await fetch(`/api/assistant/conversations/${conversationId}/messages/partial`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: trimmed, run_id: runId || undefined }),
      }))
      return true
    } catch {
      return false
    }
  }

  const togglePause = async () => {
    if (!conversationId || !sending) return
    const next = !paused
    try {
      await fetch(`/api/assistant/conversations/${conversationId}/stream/${next ? 'pause' : 'resume'}`, { method: 'POST' })
      setPaused(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    }
  }

  const interruptStream = async () => {
    if (!sending) return
    const runId = streamRunIdRef.current
    if (runId) await fetch(`/api/assistant/runs/${runId}/cancel`, { method: 'POST' }).catch(() => undefined)
    streamAbortRef.current?.abort()
  }

  const sendMessage = async (text: string) => {
    const content = text.trim()
    if (!content || sending || activeApproval) return
    let targetId = conversationId
    if (!targetId) targetId = await createConversation()
    const optimistic: ChatMessage = { id: `local-${Date.now()}`, role: 'user', content, message_type: 'text' }
	const assistantId = `stream-${Date.now()}`
	const assistantPlaceholder: ChatMessage = { id: assistantId, role: 'assistant', content: '', message_type: 'text' }
	// 用户发送后立即建立回答区域，让连接、理解和检索状态都有稳定载体。
	setMessages(current => [...current, optimistic, assistantPlaceholder])
    setInput('')
    setError('')
    setSending(true)
    setPaused(false)
    setInterrupted(false)
    const abortController = new AbortController()
    streamAbortRef.current = abortController
    streamRunIdRef.current = null
    streamedContentRef.current = ''
	let displayedContent = ''
	const flushStreamContent = () => {
	  streamRenderFrameRef.current = null
	  if (displayedContent === streamedContentRef.current) return
	  displayedContent = streamedContentRef.current
	  setMessages(current => current.map(item => item.id === assistantId ? { ...item, content: displayedContent } : item))
	}
	const scheduleStreamRender = () => {
	  if (streamRenderFrameRef.current !== null) return
	  streamRenderFrameRef.current = window.requestAnimationFrame(flushStreamContent)
	}
    try {
      const response = await fetch(`/api/assistant/conversations/${targetId}/messages/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: content, job_id: jobId || undefined, web_enabled: webEnabled }),
        signal: abortController.signal,
      })
      if (!response.ok || !response.body) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || '发送失败')
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let streamedContent = ''
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
        const events = buffer.split('\n\n')
        buffer = events.pop() || ''
        for (const event of events) {
          const dataLine = event.split('\n').find(line => line.startsWith('data: '))
          if (!dataLine) continue
          const payload = JSON.parse(dataLine.slice(6)) as { id?: string; run_id?: string; label?: string; detail?: string; delta?: string; content?: string; citations?: Citation[]; approval?: Approval | null; task?: TaskCard | null; tool_trace?: ToolTrace[]; execution_plan?: PlanStep[]; execution_events?: ExecutionEvent[]; dag_result?: DagResult | null; evidence_route?: EvidenceRoute; evidence_tasks?: EvidenceTask[]; evidence_results?: EvidenceResult[]; evidence_coverage?: { complete?: boolean; missing_task_ids?: string[] }; status?: string; error?: string }
          if (event.startsWith('event: start')) {
            streamRunIdRef.current = payload.run_id || null
          } else if (event.startsWith('event: delta')) {
            streamedContent += payload.delta || ''
            streamedContentRef.current = streamedContent
			scheduleStreamRender()
          } else if (event.startsWith('event: trace') && payload.id && payload.label && payload.status) {
            const executionEvent = payload as ExecutionEvent
            setMessages(current => current.map(item => item.id === assistantId ? {
              ...item,
              metadata: {
                ...item.metadata,
                execution_events: upsertExecutionEvent(item.metadata?.execution_events, executionEvent),
              },
            } : item))
          } else if (event.startsWith('event: done')) {
			if (streamRenderFrameRef.current !== null) {
			  window.cancelAnimationFrame(streamRenderFrameRef.current)
			  streamRenderFrameRef.current = null
			}
            if (payload.status === 'cancelled') {
              await savePartialAnswer(targetId, streamedContent, payload.run_id || streamRunIdRef.current)
              break
            }
            const finalContent = typeof payload.content === 'string' && payload.content.trim() ? payload.content : streamedContent
            const finalEvents = finalizeExecutionEvents(normalizeExecutionEvents(payload.execution_events))
            setMessages(current => current.map(item => item.id === assistantId ? {
              ...item,
			  id: payload.id || item.id,
              content: finalContent,
              message_type: payload.approval ? 'approval' : 'text',
			  metadata: { run_id: payload.run_id, citations: payload.citations, approval: payload.approval, task: payload.task, tool_trace: payload.tool_trace, execution_plan: payload.execution_plan, execution_events: finalEvents, dag_result: payload.dag_result, evidence_route: payload.evidence_route, evidence_tasks: payload.evidence_tasks, evidence_results: payload.evidence_results, evidence_coverage: payload.evidence_coverage },
            } : item))
          } else if (event.startsWith('event: error')) {
            throw new Error(payload.error || '生成回答失败')
          }
        }
        if (done) break
      }
	  // done 事件已经携带持久化消息 ID 与完整元数据，不再重载整个消息列表，
	  // 避免流式气泡被替换时产生闪动、滚动跳跃和展开态丢失。
	  await Promise.all([loadConversations(), loadMemories()])
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // 用户主动打断：保留已输出的部分回答并落库，作为后续补充提问的上下文。
        setInterrupted(true)
        const saved = await savePartialAnswer(targetId, streamedContentRef.current, streamRunIdRef.current)
        if (saved) await Promise.all([loadMessages(targetId), loadConversations()])
        else setMessages(current => current.map(item => item.id === assistantId ? { ...item, metadata: { ...item.metadata, run_id: streamRunIdRef.current || undefined } } : item))
      } else if (streamedContentRef.current.trim()) {
        // 生成中途失败：保留用户消息和部分回答，不静默清空。
        setMessages(current => current.filter(item => item.id !== optimistic.id && item.id !== assistantId))
        await savePartialAnswer(targetId, streamedContentRef.current, streamRunIdRef.current)
        setInput(content)
        setError(err instanceof Error ? err.message : '发送失败')
      } else {
        setMessages(current => current.filter(item => item.id !== optimistic.id && !item.id.startsWith('stream-')))
        setInput(content)
        setError(err instanceof Error ? err.message : '发送失败')
      }
    } finally {
	  if (streamRenderFrameRef.current !== null) {
		window.cancelAnimationFrame(streamRenderFrameRef.current)
		streamRenderFrameRef.current = null
	  }
      setSending(false)
      setPaused(false)
      streamAbortRef.current = null
    }
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    sendMessage(input)
  }
  const resolveApproval = async (action: 'approve' | 'edit' | 'reject', approval?: Approval, selectedItemIds: string[] = [], editedReply?: string) => {
    if (!conversationId || sending) return
    setSending(true)
    setError('')
    try {
      await readJson(await fetch(`/api/assistant/conversations/${conversationId}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, action_id: approval?.action_id, sql_hash: approval?.sql_hash, selected_item_ids: selectedItemIds, edited_reply: editedReply || (action === 'edit' ? editingDraft : undefined) }),
      }))
      setEditingDraft('')
      setResolutionNotice(action === 'edit'
        ? '已提交修改。修改后的提案必须再次确认才会执行。'
        : action === 'approve'
          ? '已确认，执行结果会记录在下方的执行过程中。'
          : '已取消，不会执行任何动作。')
      await Promise.all([loadMessages(conversationId), loadConversations()])
    } catch (err) {
      setError(err instanceof Error ? err.message : '确认失败')
    } finally {
      setSending(false)
    }
  }
  const forgetMemory = async (id: string) => {
    try {
      await readJson(await fetch(`/api/assistant/memories/${id}`, { method: 'DELETE' }))
      await loadMemories()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除记忆失败')
    }
  }
  const deleteConversations = async (ids: string[]) => {
    if (!ids.length) return
    const confirmed = await confirm({
      title: `删除 ${ids.length} 个对话`,
      description: '该对话的消息、执行记录与引用会一并移除。',
      confirmLabel: `删除 ${ids.length} 个对话`,
      cancelLabel: '保留',
      tone: 'danger',
      wont: ['不会删除其它对话', '不会影响已投递岗位的状态'],
    })
    if (!confirmed) return
    try {
      await readJson(await fetch('/api/assistant/conversations', {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }),
      }))
      const remaining = await loadConversations()
      setSelectedConversations([])
      if (ids.includes(conversationId)) {
        if (remaining.length) { setConversationId(remaining[0].id); await loadMessages(remaining[0].id) }
        else { await createConversation() }
      }
    } catch (err) { setError(err instanceof Error ? err.message : '删除对话失败') }
  }

  return (
	<div className="assistant-shell grid h-full min-h-0 grid-cols-1 overflow-hidden xl:grid-cols-[232px_minmax(0,1fr)_256px] 2xl:grid-cols-[260px_minmax(0,1fr)_288px]">
      <aside className="hidden min-h-0 border-r border-card-border bg-surface p-4 xl:flex xl:flex-col">
        <div className="mb-5 flex items-center justify-between">
          <div className="text-sm font-semibold text-foreground">求职对话</div>
          <div className="flex items-center gap-1">
            <Button size="icon" variant="ghost" title={conversationSelectMode ? '退出选择模式' : '批量删除对话'} onClick={() => { setConversationSelectMode(mode => !mode); setSelectedConversations([]) }}>{conversationSelectMode ? <X className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}</Button>
            {conversationSelectMode && selectedConversations.length > 0 && <Button size="icon" variant="ghost" title="删除选中对话" onClick={() => deleteConversations(selectedConversations)}><Trash2 className="h-3.5 w-3.5 text-danger" /></Button>}
            <Button size="icon" variant="secondary" title="新建对话" onClick={createConversation}><Plus className="h-4 w-4" /></Button>
          </div>
        </div>
		<div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-1">
          {conversations.map(item => (
            <div key={item.id} className={`group flex items-start gap-2 rounded-xl border px-2 py-2 transition ${item.id === conversationId ? 'border-primary/30 bg-primary/10' : 'border-transparent hover:border-card-border hover:bg-surface-raised'}`}>
            {conversationSelectMode && <button onClick={() => setSelectedConversations(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} className={`mt-2 h-4 w-4 rounded-full border ${selectedConversations.includes(item.id) ? 'border-primary bg-primary' : 'border-card-border'}`} aria-label={`选择对话 ${item.title}`} />}
            <button onClick={() => conversationSelectMode ? setSelectedConversations(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id]) : (() => { setConversationId(item.id); setError(''); loadMessages(item.id) })()}
              className="min-w-0 flex-1 py-1 text-left">
              <div className="truncate text-xs font-medium text-foreground">{item.title}</div>
              <div className="mt-1 truncate text-xs text-text-tertiary">{item.preview || '等待第一条消息'}</div>
            </button><button onClick={() => deleteConversations([item.id])} title="删除对话" className="mt-1 p-1 opacity-0 transition group-hover:opacity-100 focus-visible:opacity-100 group-focus-within:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"><X className="h-3.5 w-3.5 text-text-tertiary hover:text-danger" /></button>
            </div>
          ))}
        </div>
		<div className="mt-4 shrink-0 rounded-xl border border-primary/15 bg-primary/[0.04] p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-primary"><ShieldCheck className="h-3.5 w-3.5" />人工确认已启用</div>
          <p className="mt-2 text-xs leading-4 text-text-tertiary">Agent 可以分析和生成草稿，但不会绕过确认执行敏感操作。</p>
        </div>
      </aside>

      <main className="relative flex min-h-0 overflow-hidden flex-col">
        <div className="border-b border-card-border bg-surface px-5 py-4">
		  <div className="mx-auto flex max-w-4xl items-center justify-between gap-4">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-primary/30 bg-primary/10"><Bot className="h-5 w-5 text-primary" /></div>
              <div className="min-w-0">
                <h2 className="truncate text-sm font-semibold text-foreground">BossHunter Agent</h2>
                <p className="mt-0.5 text-xs text-text-tertiary">岗位数据 · 当前简历 · 长期记忆 · HR 回复</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <div className={`hidden rounded-full border px-2.5 py-1 text-xs sm:block ${health.online ? 'border-success/20 bg-success/5 text-success' : 'border-danger/25 bg-danger/5 text-danger'}`} title={health.online ? `后端已连接${health.version ? ` · v${health.version}` : ''}` : '无法连接本地后端，请确认服务是否在运行'}>{health.online ? '后端已连接' : '后端未连接'}</div>
              <Button size="icon" variant="secondary" className="xl:hidden" onClick={createConversation}><Plus className="h-4 w-4" /></Button>
            </div>
          </div>
        </div>

		<div
			ref={scrollRef}
			onScroll={event => {
				const container = event.currentTarget
				shouldFollowLatestRef.current = container.scrollHeight - container.scrollTop - container.clientHeight < 120
				if (container.scrollTop < 200) void loadOlderMessages()
			}}
			className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6"
		>
		  <div className="mx-auto max-w-4xl">
			{hasOlderMessages && (
				<div className="mb-4 flex justify-center">
					<Button size="sm" variant="ghost" disabled={loadingOlderMessages} onClick={() => void loadOlderMessages()}>
						{loadingOlderMessages ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
						{loadingOlderMessages ? '加载中…' : '加载更早消息'}
					</Button>
				</div>
			)}
            {!messages.length && (
			  <div className="py-8 sm:py-12">
				<div className="max-w-2xl">
                  <h3 className="text-2xl font-semibold leading-tight text-foreground sm:text-3xl">不只是回答问题，<span className="text-primary">而是理解你的求职进度。</span></h3>
                  <p className="mt-4 max-w-lg text-sm leading-6 text-text-secondary">我可以读取本地岗位数据库和当前简历，延续多轮对话，并在生成 HR 回复后等待你确认。</p>
                </div>
                <div className="mt-8 grid gap-2 sm:grid-cols-2">
                  {quickPrompts.map(prompt => (
                    <button key={prompt} onClick={() => sendMessage(prompt)} className="group flex items-center justify-between rounded-xl border border-card-border bg-surface px-4 py-3 text-left text-xs text-text-secondary transition hover:border-primary/30 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
                      <span>{prompt}</span><ChevronRight className="h-3.5 w-3.5 text-text-tertiary transition group-hover:translate-x-0.5 group-hover:text-primary" />
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="space-y-6">
			  {messages.map((message, index) => (
				<div key={message.id} style={{ contentVisibility: 'auto', containIntrinsicSize: '0 180px' }}>
					<MessageBubble message={message} conversationId={conversationId} approvalActive={message.id === activeApprovalMessageId} approvalOutcome={approvalOutcomeFor(index)} editingDraft={editingDraft} setEditingDraft={setEditingDraft} resolveApproval={resolveApproval} busy={sending} isStreaming={sending && message.id.startsWith('stream-')} />
				</div>
			  ))}
              {sending && !messages.some(message => message.id.startsWith('stream-')) && <div className="flex items-center gap-3 text-xs text-text-tertiary"><div className="grid h-8 w-8 place-items-center rounded-lg border border-primary/20 bg-primary/5"><Loader2 className="h-3.5 w-3.5 animate-spin text-primary" /></div>{paused ? '输出已暂停，点击“继续”恢复' : '正在连接 Agent…'}</div>}
              {sending && paused && messages.some(message => message.id.startsWith('stream-')) && <div className="flex items-center gap-2 text-xs text-warning"><Pause className="h-3.5 w-3.5" />输出已暂停，生成仍在后台进行，恢复后会补齐剩余内容。</div>}
            </div>
          </div>
        </div>

		<div className="shrink-0 border-t border-card-border bg-surface px-4 py-3 sm:px-6">
		  <form onSubmit={handleSubmit} className="mx-auto max-w-4xl">
            {error && <div className="mb-2 rounded-lg border border-danger/25 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
            {resolutionNotice && <div className="mb-2 flex items-center gap-2 rounded-lg border border-info/25 bg-info/5 px-3 py-2 text-xs text-info">{resolutionNotice}<button type="button" onClick={() => setResolutionNotice('')} className="ml-auto rounded p-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40" aria-label="关闭提示"><X className="h-3 w-3" /></button></div>}
            {interrupted && !sending && <div className="mb-2 rounded-lg border border-warning/25 bg-warning/5 px-3 py-2 text-xs text-warning">已打断本轮回答。可以输入补充内容后重新提问，助手会结合已生成的部分继续作答。</div>}
            {activeApproval && <div className="mb-2 flex items-center gap-2 rounded-lg border border-warning/25 bg-warning/5 px-3 py-2 text-xs text-warning"><ShieldCheck className="h-3.5 w-3.5" />请先处理上方待确认的{approvalNoun(activeApproval)}，再继续对话。</div>}
            <div className="rounded-2xl border border-card-border-strong bg-surface p-2 shadow-card transition focus-within:border-primary">
              <Textarea value={input} onChange={event => setInput(event.target.value)}
                onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); handleSubmit(event) } }}
                disabled={sending || Boolean(activeApproval)} placeholder={activeApproval ? `请先完成${approvalNoun(activeApproval)}确认` : '询问岗位、简历、求职策略，或粘贴 HR 消息…'}
                className="min-h-[68px] resize-none border-0 bg-transparent shadow-none focus:ring-0" />
              <div className="flex items-center justify-between gap-3 px-1 pb-1">
                <div className="flex min-w-0 items-center gap-2">
                  {sending && (
                    <>
                      <Button type="button" size="sm" variant="ghost" onClick={togglePause} className="h-8 gap-1.5 px-2 text-xs" title={paused ? '继续输出回答' : '暂停输出回答'}>
                        {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
                        {paused ? '继续' : '暂停'}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={interruptStream} className="h-8 gap-1.5 px-2 text-xs text-danger hover:text-danger" title="停止生成，保留已输出内容并可补充说明后重新提问">
                        <Square className="h-3.5 w-3.5" />
                        打断并补充
                      </Button>
                    </>
                  )}
                  <Select value={jobId} onChange={event => setJobId(event.target.value)} className="h-8 max-w-[210px] border-0 bg-transparent text-xs">
                    <option value="">自动选择岗位上下文</option>
                    {jobs.map(job => <option key={job.id} value={job.id}>{job.company} · {job.title}</option>)}
                  </Select>
                </div>
                <Button size="icon" disabled={!input.trim() || sending || Boolean(activeApproval)} title="发送"><Send className="h-4 w-4" /></Button>
              </div>
            </div>
            <p className="mt-2 text-center text-xs text-text-tertiary">Agent 可能出错；涉及个人经历与 HR 回复时请核对事实。</p>
          </form>
        </div>
      </main>

	  <aside className="hidden min-h-0 flex-col border-l border-card-border bg-surface p-4 xl:flex">
        <div className="mb-6">
          <div className="flex items-center gap-2 text-xs font-semibold text-foreground"><Brain className="h-3.5 w-3.5 text-primary" />长期记忆<span className="ml-auto rounded-full bg-surface-raised px-2 py-0.5 text-xs text-text-tertiary">{memories.length}</span></div>
          <p className="mt-2 text-xs leading-4 text-text-tertiary">只有明确说“记住……”的内容才会自动保存，你可以随时删除。</p>
        </div>
		<div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
          {!memories.length && <div className="rounded-xl border border-dashed border-card-border p-4 text-center text-xs text-text-tertiary">暂无长期记忆</div>}
          {memories.map(memory => (
            <div key={memory.id} className="group rounded-xl border border-card-border bg-surface-raised p-3">
              <div className="flex items-start gap-2"><div className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" /><p className="flex-1 text-xs leading-5 text-muted-foreground">{memory.content}</p>
                <button onClick={() => forgetMemory(memory.id)} className="opacity-0 transition group-hover:opacity-100 focus-visible:opacity-100 group-focus-within:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40" title="删除记忆"><Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-danger" /></button>
              </div>
            </div>
          ))}
        </div>
		<div className="mt-5 shrink-0 border-t border-card-border pt-5">
          <div className="mb-3 text-xs font-semibold text-foreground">本轮能力</div>
          <div className="space-y-2 text-xs text-muted-foreground">
            {latestCapabilities.length
              ? latestCapabilities.map(name => {
                  const entry = capabilityRows[name] || { label: name, icon: Bot }
                  return <Capability key={name} icon={entry.icon} label={entry.label} active />
                })
              : <p className="text-xs leading-4 text-text-tertiary">本轮尚未记录能力调用。</p>}
          </div>
        </div>
      </aside>
    </div>
  )
}

const MessageBubble = memo(function MessageBubble({ message, conversationId, approvalActive, approvalOutcome, editingDraft, setEditingDraft, resolveApproval, busy, isStreaming }: {
  message: ChatMessage; editingDraft: string; setEditingDraft: (value: string) => void
  conversationId: string
  approvalActive: boolean
  approvalOutcome: ApprovalOutcome
	isStreaming: boolean
  resolveApproval: (action: 'approve' | 'edit' | 'reject', approval?: Approval, selectedItemIds?: string[], editedReply?: string) => void; busy: boolean
}) {
  const isUser = message.role === 'user'
  const approval = message.metadata?.approval
  const approvalChip = approvalOutcomeChip(approvalOutcome)
  const task = message.metadata?.task
	const hasProcessDetails = !isUser && (
	  Boolean(message.metadata?.execution_events?.length)
	  || Boolean(message.metadata?.tool_trace?.length)
	  || Boolean(message.metadata?.execution_plan?.length)
	  || Boolean(message.metadata?.evidence_tasks?.length)
	  || Boolean(task)
	)
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(message.content)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }
  return (
	<div className={`group/message flex ${isUser ? 'justify-end' : 'justify-start'}`}>
	  <div className={`min-w-0 ${isUser ? 'max-w-[88%] sm:max-w-[72%]' : 'w-full max-w-[48rem]'}`}>
		{hasProcessDetails && <div className="mb-3">
		  <ExecutionRecord
			events={message.metadata?.execution_events || []}
			traces={message.metadata?.tool_trace || []}
			steps={message.metadata?.execution_plan || []}
			dagResult={message.metadata?.dag_result}
			runId={message.metadata?.run_id}
			task={task}
			approvalResolved={Boolean(approval && !approvalActive)}
			evidenceRoute={message.metadata?.evidence_route}
			evidenceTasks={message.metadata?.evidence_tasks || []}
			evidenceResults={message.metadata?.evidence_results || []}
			evidenceCoverage={message.metadata?.evidence_coverage}
		  />
		</div>}
		<div className={isUser ? 'rounded-2xl rounded-tr-md border border-card-border bg-surface-raised px-4 py-2.5 text-sm leading-6 text-foreground shadow-[0_1px_1px_rgb(15_23_42/0.025)]' : 'text-foreground'}>
          {isUser
			? message.content
			: message.content
			  ? <div className={`assistant-markdown ${isStreaming ? 'assistant-markdown--streaming' : ''}`} aria-busy={isStreaming} dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content, message.metadata?.citations) }} />
			  : isStreaming && !hasProcessDetails
				? <div className="inline-flex min-h-8 items-center gap-2 text-xs text-text-secondary" role="status" aria-live="polite"><span className="status-pulse h-1.5 w-1.5 rounded-full bg-primary" />正在理解你的问题…</div>
				: null}
        </div>
		{!isUser && message.message_type === 'text' && !task && !approval && message.metadata?.citations && message.metadata.citations.length > 0 && <CitationList citations={message.metadata.citations} />}
        {approval && (
          <div className={`mt-4 overflow-hidden rounded-2xl border ${approvalActive ? 'border-warning/30 bg-warning/5' : 'border-card-border bg-surface opacity-70'}`}>
            <div className={`flex items-center gap-2 border-b px-4 py-3 text-xs font-medium ${approvalActive ? 'border-warning/20 text-warning' : 'border-card-border text-text-tertiary'}`}><ShieldCheck className="h-4 w-4" />{approval.title}<Badge variant={toneBadgeVariant[approvalChip.tone]} className="ml-auto font-normal">{approvalChip.label}</Badge></div>
            <div className="p-4">
              <ApprovalCard approval={approval} active={approvalActive} contextText={message.content} editingDraft={editingDraft} setEditingDraft={setEditingDraft} resolveApproval={resolveApproval} busy={busy} />
            </div>
          </div>
        )}
        {!isUser && <MessageFeedback conversationId={conversationId} message={message} onCopy={!approval ? copy : undefined} copied={copied} />}
      </div>
    </div>
  )
})

const badCaseCategories = [
  { value: 'intent_mismatch', label: '理解错了我的意图' },
  { value: 'routing_error', label: '选错了工具或执行路径' },
  { value: 'incorrect_answer', label: '回答内容不准确' },
  { value: 'context_missed', label: '忽略了对话或岗位上下文' },
  { value: 'unsafe_action', label: '操作建议不安全' },
  { value: 'other', label: '其他问题' },
]

function MessageFeedback({ conversationId, message, onCopy, copied }: {
  conversationId: string
  message: ChatMessage
  onCopy?: () => void
  copied: boolean
}) {
  const [rating, setRating] = useState<'helpful' | 'unhelpful' | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [category, setCategory] = useState('intent_mismatch')
  const [expectedIntent, setExpectedIntent] = useState('')
  const [feedback, setFeedback] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [feedbackError, setFeedbackError] = useState('')

  const rate = async (nextRating: 'helpful' | 'unhelpful') => {
    if (message.id.startsWith('stream-') || message.id.startsWith('local-')) return
    setFeedbackError('')
    try {
      await readJson(await fetch('/api/assistant/message-ratings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: conversationId, message_id: message.id, rating: nextRating }),
      }))
      setRating(nextRating)
      setShowForm(nextRating === 'unhelpful')
      if (nextRating === 'helpful') setSubmitted(false)
    } catch (err) {
      setFeedbackError(err instanceof Error ? err.message : '评价保存失败')
    }
  }

  const submitBadCase = async () => {
    setSubmitting(true)
    setFeedbackError('')
    try {
      await readJson(await fetch('/api/assistant/bad-cases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: conversationId,
          message_id: message.id,
          category,
          expected_intent: expectedIntent,
          feedback,
        }),
      }))
      setSubmitted(true)
      setShowForm(false)
    } catch (err) {
      setFeedbackError(err instanceof Error ? err.message : 'Bad Case 保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  const disabled = message.id.startsWith('stream-') || message.id.startsWith('local-')
  return <div className="mt-2.5">
	<div className={`flex items-center gap-1 transition-opacity duration-150 ${rating || submitted || showForm ? 'opacity-100' : 'opacity-0 group-hover/message:opacity-100 group-focus-within/message:opacity-100'}`} aria-label="评价这条回答">
	  {onCopy && <button onClick={onCopy} aria-label={copied ? '已复制回答' : '复制回答'} title={copied ? '已复制' : '复制'} className="rounded-md p-1.5 text-text-tertiary transition hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">{copied ? <Check className="h-3.5 w-3.5" /> : <Clipboard className="h-3.5 w-3.5" />}</button>}
      <button type="button" onClick={() => rate('helpful')} disabled={disabled} aria-label="这条回答有帮助" aria-pressed={rating === 'helpful'} title="有帮助" className={`rounded-md p-1.5 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${rating === 'helpful' ? 'bg-success/10 text-success' : 'text-text-tertiary hover:bg-surface-raised hover:text-foreground'} disabled:cursor-wait disabled:opacity-40`}><ThumbsUp className="h-3.5 w-3.5" /></button>
      <button type="button" onClick={() => rate('unhelpful')} disabled={disabled} aria-label="这条回答有问题" aria-pressed={rating === 'unhelpful'} title="有问题" className={`rounded-md p-1.5 transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 ${rating === 'unhelpful' ? 'bg-danger/10 text-danger' : 'text-text-tertiary hover:bg-surface-raised hover:text-foreground'} disabled:cursor-wait disabled:opacity-40`}><ThumbsDown className="h-3.5 w-3.5" /></button>
      {rating === 'helpful' && <span className="ml-1 text-xs text-success">感谢反馈</span>}
      {submitted && <span className="ml-1 text-xs text-success">已加入 Bad Case</span>}
    </div>
    {showForm && !submitted && <div className="mt-2 max-w-xl rounded-xl border border-card-border bg-surface p-3">
      <div className="mb-3">
        <p className="text-xs font-medium text-foreground">这条回答哪里有问题？</p>
        <p className="mt-1 text-xs leading-4 text-text-tertiary">系统会自动附带原问题、识别意图和路由轨迹。</p>
      </div>
      <label className="block text-xs font-medium text-text-secondary">问题类型
        <Select value={category} onChange={event => setCategory(event.target.value)} className="mt-1 h-9 text-xs">
          {badCaseCategories.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </Select>
      </label>
      <label className="mt-3 block text-xs font-medium text-text-secondary">你期望它理解成什么？（选填）
        <input value={expectedIntent} onChange={event => setExpectedIntent(event.target.value)} maxLength={120} placeholder="例如：只查询岗位，不要启动采集" className="mt-1 h-9 w-full rounded-lg border border-card-border bg-surface px-3 text-xs text-foreground outline-none placeholder:text-text-tertiary focus:border-primary" />
      </label>
      <label className="mt-3 block text-xs font-medium text-text-secondary">正确回答或补充说明（选填）
        <Textarea value={feedback} onChange={event => setFeedback(event.target.value)} maxLength={2000} placeholder="告诉我们正确的处理方式" className="mt-1 min-h-[72px] text-xs" />
      </label>
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" onClick={submitBadCase} disabled={submitting}>{submitting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}提交问题</Button>
        <Button size="sm" variant="ghost" onClick={() => setShowForm(false)} disabled={submitting}>暂不补充</Button>
      </div>
    </div>}
    {feedbackError && <p className="mt-1 text-xs text-danger">{feedbackError}</p>}
  </div>
}

function ApprovalCard({ approval, active, contextText, editingDraft, setEditingDraft, resolveApproval, busy }: {
  approval: Approval
  active: boolean
  contextText?: string
  editingDraft: string
  setEditingDraft: (value: string) => void
  resolveApproval: (action: 'approve' | 'edit' | 'reject', approval?: Approval, selectedItemIds?: string[], editedReply?: string) => void
  busy: boolean
}) {
  const selectable = approval.type === 'send_greetings' || approval.type === 'send_hr_reply'
  const items = approval.affected_items || []
  const [selected, setSelected] = useState<string[]>([])
  const reply = editingDraft || approval.affected_items?.[0]?.message || approval.draft || ''
	const isSqlApproval = approval.type === 'execute_readonly_sql'
	const sqlDraft = editingDraft || approval.sql || ''
  const toggle = (id: string) => setSelected(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id])
  const toggleAll = () => setSelected(current => current.length === items.length ? [] : items.map(item => item.id))
  return <>
    {approval.summary && !(contextText || '').includes(approval.summary) && <p className="mb-3 text-xs leading-5 text-muted-foreground">{approval.summary}</p>}
	{isSqlApproval && (
		<div className="mb-3 overflow-hidden rounded-xl border border-card-border bg-[#111827] shadow-inner">
			<div className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-xs text-slate-400">
				<span className="flex items-center gap-1.5 font-medium uppercase tracking-[0.14em]"><Code2 className="h-3.5 w-3.5 text-emerald-400" />PostgreSQL · 只读审批</span>
				<button type="button" onClick={() => navigator.clipboard.writeText(sqlDraft)} className="rounded px-1 transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">复制 SQL</button>
			</div>
			<textarea value={sqlDraft} onChange={event => setEditingDraft(event.target.value)} disabled={!active} spellCheck={false} aria-label="待执行 SQL" className="min-h-[180px] w-full resize-y bg-transparent px-4 py-3 font-mono text-xs leading-6 text-emerald-100 outline-none disabled:cursor-not-allowed" />
			<div className="flex flex-wrap gap-2 border-t border-white/10 px-3 py-2 text-xs text-slate-400">
				<span>表：{approval.tables?.join('、') || '岗位数据'}</span><span>·</span>
				<span>最多 {approval.row_limit || 200} 条</span><span>·</span>
				<span>超时 {approval.timeout_seconds || 10} 秒</span>
			</div>
			{Boolean(approval.parameters?.length) && <div className="border-t border-white/10 px-3 py-2 text-xs text-slate-400">参数：<code className="text-slate-200">{JSON.stringify(approval.parameters)}</code></div>}
		</div>
	)}
    {selectable && items.length > 1 && <div className="mb-2 flex items-center gap-2 text-xs text-text-tertiary">
      <span>已选 {selected.length}/{items.length}</span>
      <button type="button" onClick={toggleAll} disabled={!active} className="rounded px-1.5 py-0.5 font-medium text-primary transition hover:bg-primary/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-not-allowed disabled:opacity-45">{selected.length === items.length ? '全不选' : '全选'}</button>
    </div>}
    {items.map(item => {
      const id = approval.type === 'send_hr_reply' ? item.id : item.id
      return <label key={id} className="mb-2 flex gap-2 rounded-lg border border-card-border bg-surface p-3 text-xs">
        {selectable && <input type="checkbox" checked={selected.includes(id)} onChange={() => toggle(id)} disabled={!active} className="mt-0.5 rounded border-card-border-strong text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-not-allowed" />}
        <span className="min-w-0"><span className="font-medium text-foreground">{item.company} · {item.title}</span>{item.greeting && <span className="mt-1 block whitespace-pre-wrap text-muted-foreground">{item.greeting}</span>}{item.detail && <span className="mt-1 block text-muted-foreground">{item.detail}</span>}</span>
      </label>
    })}
    {approval.type === 'send_hr_reply' || approval.type === 'reply_draft_approval' ? <Textarea value={reply} onChange={event => setEditingDraft(event.target.value)} disabled={!active} className="min-h-[110px] bg-surface text-sm leading-6 disabled:cursor-not-allowed disabled:opacity-60" /> : null}
    <div className="mt-3 flex flex-wrap gap-2">
      <Button size="sm" onClick={() => resolveApproval('approve', approval, selectable ? selected : [], reply)} disabled={!active || busy || (selectable && selected.length === 0)} className="disabled:cursor-not-allowed disabled:opacity-40"><Check className="mr-1.5 h-3.5 w-3.5" />{active ? '确认并执行' : '已处理'}</Button>
	  {isSqlApproval && <Button size="sm" variant="secondary" onClick={() => resolveApproval('edit', approval, [], sqlDraft)} disabled={!active || busy || sqlDraft.trim() === approval.sql?.trim()}>提交修改并重新校验</Button>}
      <Button size="sm" variant="ghost" onClick={() => resolveApproval('reject', approval)} disabled={!active || busy} className="disabled:cursor-not-allowed disabled:opacity-40">取消</Button>
    </div>
	<p className="mt-3 text-xs text-text-tertiary">{isSqlApproval ? '确认前不会执行 SQL；修改后必须重新通过只读校验并再次确认。' : '确认后才会启动任务；发送仍受频率、窗口和平台安全校验约束。'}</p>
  </>
}

function formatDuration(durationMs?: number): string {
  if (durationMs === undefined || durationMs < 0) return ''
  if (durationMs < 1000) return `${durationMs} 毫秒`
  return `${(durationMs / 1000).toFixed(durationMs < 10000 ? 1 : 0)} 秒`
}

type BusinessStageStatus = ExecutionStatus | 'partial' | 'insufficient' | 'cancelled'

/**
 * 任务状态的唯一色彩来源。
 * 原先 eventStatusClass / businessStageStatusClass / JSX 手写 chip 三套并存，
 * 同一状态在不同位置颜色不一致（例如 stopped 一处 danger、一处 warning）。
 */
type StatusTone = 'success' | 'danger' | 'warning' | 'primary' | 'neutral' | 'muted'

function statusTone(status: BusinessStageStatus): StatusTone {
	if (status === 'completed') return 'success'
	if (status === 'failed') return 'danger'
	if (status === 'partial' || status === 'insufficient') return 'warning'
	if (status === 'waiting' || status === 'approval_required' || status === 'awaiting_approval') return 'warning'
	// 取消与停止是用户主动中断，不是失败，因此归 warning 而非 danger。
	if (status === 'cancelled' || status === 'stopped') return 'warning'
	if (status === 'running' || status === 'started' || status === 'stopping') return 'primary'
	if (status === 'incomplete') return 'neutral'
	return 'muted'
}

const statusToneClass: Record<StatusTone, string> = {
	success: 'border-success/35 bg-success/10 text-success',
	danger: 'border-danger/35 bg-danger/10 text-danger',
	warning: 'border-warning/35 bg-warning/10 text-warning',
	primary: 'border-primary/40 bg-primary/10 text-primary',
	neutral: 'border-card-border-strong bg-surface-raised text-text-secondary',
	muted: 'border-card-border bg-surface-raised text-text-tertiary',
}

function statusChipClass(status: BusinessStageStatus): string {
	return statusToneClass[statusTone(status)]
}

/** 图标本身也要能区分语义，不能只靠颜色。 */
function StatusIcon({ status, className }: { status: BusinessStageStatus; className: string }) {
	if (status === 'completed') return <Check className={className} />
	if (status === 'failed' || status === 'cancelled' || status === 'stopped') return <X className={className} />
	if (status === 'skipped') return <Minus className={className} />
	if (status === 'incomplete') return <span className="text-xs font-bold leading-none" aria-hidden="true">?</span>
	if (status === 'partial' || status === 'insufficient' || status === 'waiting' || status === 'approval_required' || status === 'awaiting_approval') return <ShieldCheck className={className} />
	if (status === 'running' || status === 'started' || status === 'stopping') return <Loader2 className={`${className} animate-spin motion-reduce:animate-none`} />
	return <span className="h-1.5 w-1.5 rounded-full bg-current" />
}
interface BusinessStage { id: string; label: string; status: BusinessStageStatus; summary?: string; durationMs?: number }

const evidenceStageConfig: Record<string, { id: string; label: string }> = {
  structured_data: { id: 'analytics', label: '查询岗位数据' },
  private_document: { id: 'private-docs', label: '分析简历与岗位' },
  public_current: { id: 'web-research', label: '搜索公开信息' },
}

const taskStageLabels: Record<string, string> = {
  full: '执行求职流程',
  collect: '采集岗位',
  score: '评估岗位',
  reenrich: '补充企业信息',
  greeting: '生成招呼语',
  monitor: '监测 HR 回复',
  deliver: '发送招呼语',
  reply: '发送 HR 回复',
  import: '定向采集岗位',
}

function stageStatus(statuses: Array<BusinessStageStatus | undefined>): BusinessStageStatus {
  const values = statuses.filter(Boolean) as BusinessStageStatus[]
  if (!values.length) return 'pending'
  if (values.some(status => status === 'failed')) return 'failed'
  if (values.some(status => status === 'partial' || status === 'insufficient')) return 'partial'
  if (values.some(status => status === 'waiting' || status === 'approval_required' || status === 'awaiting_approval')) return 'waiting'
  if (values.some(status => status === 'running' || status === 'started' || status === 'stopping')) return 'running'
  if (values.some(status => status === 'cancelled' || status === 'stopped')) return 'cancelled'
  if (values.some(status => status === 'incomplete')) return 'incomplete'
  // 「跳过」意味着这一步没有执行，不能与「已完成」合并成同一个对勾。
  if (values.some(status => status === 'skipped')) return 'skipped'
  if (values.every(status => status === 'completed')) return 'completed'
  return 'pending'
}

function deriveBusinessStages(
  events: ExecutionEvent[],
  steps: PlanStep[],
  route?: EvidenceRoute,
  evidenceTasks: EvidenceTask[] = [],
  evidenceResults: EvidenceResult[] = [],
  coverage?: { complete?: boolean; missing_task_ids?: string[] },
  task?: TaskCard | null,
): BusinessStage[] {
  const normalized = normalizeExecutionEvents(events)
  const understandingEvents = normalized.filter(event => event.phase === 'understanding' || event.id === 'evidence-route')
  const stages: BusinessStage[] = [{
    id: 'understanding',
    label: '思考',
    status: stageStatus(understandingEvents.map(event => event.status)),
    summary: route?.reason || understandingEvents[understandingEvents.length - 1]?.summary,
    durationMs: understandingEvents.reduce((total, event) => total + (event.duration_ms || 0), 0),
  }]
  const domains = route?.domains?.length
    ? route.domains
    : evidenceTasks.map(task => task.evidence_type).filter(domain => domain in evidenceStageConfig)
  const taskTypeById = new Map(evidenceTasks.map(task => [task.id, task.evidence_type]))
  const domainOfResult = (result: EvidenceResult): EvidenceDomain | undefined =>
    taskTypeById.get(result.task_id) || executorDomain[result.executor]
  const domainOfStep = (step: PlanStep): EvidenceDomain | undefined => {
    const declared = step.arguments?.evidence_type
    if (typeof declared === 'string' && declared in evidenceStageConfig) return declared as EvidenceDomain
    return taskTypeById.get(step.id) || (step.capability ? capabilityDomain[step.capability] : undefined)
  }
  Array.from(new Set(domains)).forEach(domain => {
    const config = evidenceStageConfig[domain]
    if (!config) return
    // 只使用后端声明的字段（task.evidence_type / step.arguments.evidence_type / executor / capability）
    // 归属证据域。原先的 includes() 子串猜测会在执行器改名时把证据静默丢进错误的阶段。
    const domainResults = evidenceResults.filter(result => domainOfResult(result) === domain)
    const domainSteps = steps.filter(step => domainOfStep(step) === domain)
    const resultStatuses = domainResults.map(result => result.status as BusinessStageStatus)
    const missing = domainResults.flatMap(result => result.missing_evidence || [])
    const baseSummary = domainResults.map(result => result.summary).filter(Boolean).join('；')
      || domainSteps[domainSteps.length - 1]?.label
      || (domain === 'public_current' ? '只研究筛选后的入围公司' : undefined)
    stages.push({
      ...config,
      status: stageStatus([...resultStatuses, ...domainSteps.map(step => step.status)]),
      summary: missing.length ? `${baseSummary ? `${baseSummary}；` : ''}缺少 ${missing.length} 项必要证据` : baseSummary,
    })
  })
  if (task) {
    const taskStatus = task.status === 'running'
      ? 'running'
      : task.status === 'stopping'
        ? 'stopping'
        : task.status === 'completed'
          ? 'completed'
          : task.status === 'failed'
            ? 'failed'
            : task.status === 'stopped'
              ? 'stopped'
              : 'pending'
    stages.push({
      id: `task-${task.id}`,
      label: taskStageLabels[task.mode] || task.label || '执行任务',
      status: taskStatus,
      summary: task.error || task.logs[task.logs.length - 1],
    })
  }
  const synthesisEvents = normalized.filter(event => event.phase === 'generation' || event.phase === 'finalization' || ['execution', 'result'].includes(event.id))
  const synthesisStatus = stageStatus(synthesisEvents.map(event => event.status))
  stages.push({
    id: 'synthesis',
    label: '整理答案',
    status: coverage?.complete === false && synthesisStatus === 'completed' ? 'partial' : synthesisStatus,
    summary: coverage?.complete === false
      ? `缺少 ${coverage.missing_task_ids?.length || 0} 项必要证据，答案将明确说明限制`
      : synthesisEvents[synthesisEvents.length - 1]?.summary,
    durationMs: synthesisEvents.reduce((total, event) => total + (event.duration_ms || 0), 0),
  })
  return stages
}

function ExecutionRecord({ events, traces, steps, dagResult, runId, task: initialTask, approvalResolved, evidenceRoute, evidenceTasks, evidenceResults, evidenceCoverage }: {
  events: ExecutionEvent[]
  traces: ToolTrace[]
  steps: PlanStep[]
  dagResult?: DagResult | null
  runId?: string
  task?: TaskCard | null
  approvalResolved: boolean
  evidenceRoute?: EvidenceRoute
  evidenceTasks: EvidenceTask[]
  evidenceResults: EvidenceResult[]
  evidenceCoverage?: { complete?: boolean; missing_task_ids?: string[] }
}) {
  const [task, setTask] = useState(initialTask)
  // 执行过程不是主角：默认折叠，只保留一行真实摘要，是否展开由用户决定。
  const [showStages, setShowStages] = useState(false)
  const [clock, setClock] = useState(() => Date.now())
  useEffect(() => setTask(initialTask), [initialTask])
  useEffect(() => {
    if (!task || ['completed', 'failed', 'stopped'].includes(task.status)) return
    const timer = window.setInterval(() => {
      fetch(`/api/workbench/task/${task.id}`)
        .then(response => response.ok ? response.json() : null)
        .then(next => { if (next) setTask(next) })
        .catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status])

  const stopTask = async () => {
	if (task) {
	  const response = await fetch(`/api/workbench/task/${task.id}/stop`, { method: 'POST' })
	  if (response.ok) setTask(await response.json())
	  return
	}
	if (runId) await fetch(`/api/assistant/runs/${runId}/cancel`, { method: 'POST' })
  }

  const planSteps = useMemo(() => steps.map(step => {
	if ((step.id === 'approve' || step.requires_approval) && approvalResolved) {
	  return { ...step, status: 'completed' as ExecutionStatus, label: '人工确认已处理' }
	}
	if (step.id !== 'execute' || !task) return step
	if (task.status === 'running') return { ...step, status: 'started' as ExecutionStatus }
	if (task.status === 'stopping') return { ...step, status: 'stopping' as ExecutionStatus }
	if (task.status === 'completed') return { ...step, status: 'completed' as ExecutionStatus }
	if (task.status === 'failed') return { ...step, status: 'failed' as ExecutionStatus }
	if (task.status === 'stopped') return { ...step, status: 'stopped' as ExecutionStatus }
	return step
  }), [steps, task, approvalResolved])
  const technicalEvents = useMemo(() => {
	const normalized = normalizeExecutionEvents(events).map(event => {
	  if (approvalResolved && event.kind === 'approval' && ['waiting', 'approval_required'].includes(event.status)) {
		return { ...event, status: 'completed' as ExecutionStatus, label: '人工确认已处理' }
	  }
	  return event
	})
	const existingText = normalized.map(event => `${event.id} ${event.label}`).join(' ')
	traces.forEach((trace, index) => {
	  if (existingText.includes(trace.name)) return
	  normalized.push({
		id: `legacy-tool-${index}-${trace.name}`,
		kind: 'tool',
		phase: trace.status === 'approval_required' ? 'approval' : 'retrieval',
		label: toolLabel(trace.name),
		status: approvalResolved && trace.status === 'approval_required' ? 'completed' : trace.status,
		tool_name: trace.name,
	  })
	})
	return normalized
  }, [events, traces, approvalResolved])
	const callEvents = useMemo(
	  () => technicalEvents.filter(event => event.kind === 'tool' || event.kind === 'agent' || event.kind === 'task'),
	  [technicalEvents],
	)
	const businessStages = useMemo(
	  () => deriveBusinessStages(technicalEvents, planSteps, evidenceRoute, evidenceTasks, evidenceResults, evidenceCoverage, task),
	  [technicalEvents, planSteps, evidenceRoute, evidenceTasks, evidenceResults, evidenceCoverage, task],
	)

  const statusSource: Array<{ status: BusinessStageStatus }> = businessStages
  const stageActive = statusSource.some(event => ['running', 'started', 'stopping'].includes(event.status))
  const taskActive = Boolean(task && ['running', 'stopping'].includes(task.status))
  const active = stageActive || taskActive
  const waiting = statusSource.some(event => ['waiting', 'approval_required', 'awaiting_approval'].includes(event.status))
  const failed = statusSource.some(event => ['failed', 'stopped'].includes(event.status))
    || Boolean(task && ['failed', 'stopped'].includes(task.status))

  useEffect(() => {
	if (!active) return
	setClock(Date.now())
	const timer = window.setInterval(() => setClock(Date.now()), 1000)
	return () => window.clearInterval(timer)
  }, [active])

	const timelineEvents = callEvents.length ? callEvents : technicalEvents
  const startedTimes = timelineEvents.map(event => Date.parse(event.started_at || '')).filter(Number.isFinite)
  const finishedTimes = timelineEvents.map(event => Date.parse(event.finished_at || '')).filter(Number.isFinite)
  const taskStartedAt = Date.parse(task?.created_at || '')
  const taskFinishedAt = Date.parse(task?.updated_at || '')
  if (Number.isFinite(taskStartedAt)) startedTimes.push(taskStartedAt)
  if (task && !taskActive && Number.isFinite(taskFinishedAt)) finishedTimes.push(taskFinishedAt)
  const startedAt = startedTimes.length ? Math.min(...startedTimes) : clock
  const finishedAt = finishedTimes.length ? Math.max(...finishedTimes) : clock
  const elapsed = task
    ? Math.max(0, (taskActive ? clock : finishedAt) - startedAt)
    : dagResult?.duration_ms || Math.max(0, (active ? clock : finishedAt) - startedAt)

  const completedStageLabels = businessStages.filter(stage => stage.status === 'completed').map(stage => stage.label)
  const activeStage = businessStages.find(stage => ['running', 'started', 'stopping'].includes(stage.status))
  const unrecordedStages = businessStages.filter(stage => stage.status === 'pending')
  const cancelled = businessStages.some(stage => stage.status === 'cancelled' || stage.status === 'stopped')
  const incomplete = businessStages.some(stage => stage.status === 'incomplete')
  const skipped = businessStages.some(stage => stage.status === 'skipped')
  const budgetExhausted = dagResult?.status === 'budget_exhausted'
  const invalidPlan = dagResult?.status === 'invalid'
  const coverageIncomplete = evidenceCoverage?.complete === false
  // 摘要必须与阶段状态一致：取消、预算耗尽、未记录都不能说成「已完成分析」。
  const summary = budgetExhausted
    ? '预算耗尽，结果可能不完整'
    : invalidPlan
      ? '执行计划无效，未完成'
      : activeStage
        ? `正在${activeStage.label}`
        : failed
          ? '部分步骤未完成'
          : cancelled
            ? '已取消，部分步骤未执行'
            : waiting
              ? '等待你的确认'
              : incomplete
                ? '部分步骤未收到终态'
                : coverageIncomplete
                  ? '已完成分析，但缺少必要证据'
                  : skipped && !completedStageLabels.length
                    ? '本次未执行任何步骤'
                    : unrecordedStages.length === businessStages.length
                      ? '本次未记录执行过程'
                      : `已完成分析${completedStageLabels.length ? `，${completedStageLabels.length} 个阶段` : ''}${unrecordedStages.length ? `（${unrecordedStages.length} 个阶段未记录）` : ''}`
	const headerTone = budgetExhausted || invalidPlan || failed
    ? { className: 'border-danger/35 bg-danger/10 text-danger', icon: <X className="h-3 w-3" /> }
    : active
      ? { className: 'border-primary/40 bg-primary/10 text-primary', icon: <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" /> }
      : cancelled
        ? { className: 'border-warning/35 bg-warning/10 text-warning', icon: <Square className="h-3 w-3" /> }
        : waiting || incomplete || coverageIncomplete
          ? { className: 'border-warning/35 bg-warning/10 text-warning', icon: <ShieldCheck className="h-3 w-3" /> }
          : unrecordedStages.length || skipped
            ? { className: 'border-card-border bg-surface-raised text-text-secondary', icon: <span className="h-1.5 w-1.5 rounded-full bg-current" /> }
            : { className: 'border-success/35 bg-success/10 text-success', icon: <Check className="h-3 w-3" /> }
	// 亚秒级耗时会快速跳动且没有决策价值，满一秒后再展示。
	const elapsedLabel = elapsed >= 1000 ? formatDuration(elapsed) : ''
	const compactSummary = active
	  ? summary
	  : waiting
		? '等待你的确认'
		: failed || cancelled || incomplete || coverageIncomplete || budgetExhausted || invalidPlan
		  ? summary
		  : elapsedLabel
			? `思考了 ${elapsedLabel}`
			: '已完成思考'

  return <div className="assistant-runlog" aria-label="Agent 执行过程">
	{/* 只有阶段摘要进入 live region：耗时每秒变化，若一并放进 live region，
	    屏幕阅读器会在任务运行期间每秒播报一次，用户无法听答案。 */}
	<span className="sr-only" aria-live="polite">{summary}</span>
	<button
	  type="button"
	  onClick={() => setShowStages(value => !value)}
	  className="group/runlog -ml-1.5 inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-md px-1.5 text-left text-xs text-text-tertiary transition-colors duration-150 hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
	  aria-expanded={showStages}
	>
	  <span className={`grid h-4 w-4 shrink-0 place-items-center rounded-full border ${headerTone.className}`}>
		{headerTone.icon}
	  </span>
	  <span className="truncate font-medium text-text-secondary">{compactSummary}</span>
	  {!active && callEvents.length > 0 && <span className="shrink-0 text-text-tertiary">· {callEvents.length} 项调用</span>}
	  {active && elapsedLabel && <span className="shrink-0 tabular-nums text-text-tertiary">· {elapsedLabel}</span>}
	  <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-text-tertiary opacity-0 transition-[opacity,transform] duration-150 group-hover/message:opacity-100 group-focus-within/message:opacity-100 group-hover/runlog:text-foreground group-focus-visible/runlog:opacity-100 motion-reduce:transition-none ${showStages ? 'rotate-180' : ''}`} aria-hidden="true" />
	</button>
	{showStages && <div className="mt-2.5 max-w-[42rem] border-l border-card-border pl-4 motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-1 motion-safe:duration-200">
	  <ol className="assistant-stage-list" aria-label="业务执行阶段">
		{businessStages.map(stage => <li key={stage.id} className="assistant-stage-item">
		  <span className={`assistant-stage-icon ${statusChipClass(stage.status)}`}><StatusIcon status={stage.status} className="h-3 w-3" /></span>
		  <span className="min-w-0 flex-1 pb-3">
			<span className="flex items-baseline gap-2">
			  <span className={`text-xs font-medium ${stage.status === 'pending' ? 'text-text-secondary' : 'text-foreground'} ${stage.status === 'skipped' ? 'line-through' : ''}`}>{stage.status === 'running' ? `正在${stage.label}` : stage.label}{statusNote(stage.status) ? `（${statusNote(stage.status)}）` : ''}</span>
			  {stage.durationMs ? <span className="ml-auto shrink-0 tabular-nums text-[11px] text-text-tertiary">{formatDuration(stage.durationMs)}</span> : null}
			</span>
			{stage.summary && <span className="assistant-stage-summary mt-0.5 max-w-[68ch] text-xs leading-5 text-text-tertiary">{stage.summary}</span>}
		  </span>
		</li>)}
	  </ol>
	  {callEvents.length > 0 && <details className="assistant-technical-details border-t border-card-border pt-2">
		<summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md px-1 py-1 text-xs font-medium text-text-secondary hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
		  查看调用详情 <span className="font-normal text-text-tertiary">{callEvents.length}</span><ChevronDown className="assistant-technical-chevron h-3 w-3" />
		</summary>
		<ol className="mt-2 space-y-2" aria-label="技术调用记录">
		  {callEvents.map(event => <li key={event.id} className={`flex items-start gap-2 text-xs ${event.parent_id ? 'ml-5' : ''}`}>
			<span className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded border ${statusChipClass(event.status)}`}><StatusIcon status={event.status} className="h-3 w-3" /></span>
			<span className="min-w-0 flex-1"><span className="flex gap-2"><span className={`truncate ${event.status === 'skipped' ? 'text-text-secondary line-through' : 'text-text-secondary'}`}>{event.label}{statusNote(event.status) ? `（${statusNote(event.status)}）` : ''}</span>{event.duration_ms !== undefined && <span className="ml-auto shrink-0 tabular-nums text-text-secondary">{formatDuration(event.duration_ms)}</span>}</span>{(event.summary || event.detail) && <span className="mt-0.5 block line-clamp-2 text-text-secondary">{event.summary || event.detail}</span>}</span>
		  </li>)}
		</ol>
	  </details>}
	</div>}
	{task?.error && <p className="mt-3 rounded-lg bg-danger/10 px-3 py-2 text-xs text-danger">{task.error}</p>}
	{(task && ['running', 'stopping'].includes(task.status) || (!task && active && runId)) && <Button size="sm" variant="ghost" className="mt-3" onClick={stopTask}>停止任务</Button>}
  </div>
}

function toolLabel(name: string): string {
  const labels: Record<string, string> = {
    job_database: '查询岗位数据库',
    web_search: '搜索公开资料',
    company_research: '研究公司公开信息',
    collect_jobs: '采集岗位',
    score_jobs: '评估岗位匹配度',
    complete_job_details: '补充岗位信息',
    generate_greetings: '生成招呼语',
    send_greetings: '发送招呼语',
    send_hr_reply: '发送 HR 回复',
    monitor_replies: '监测 HR 回复',
    run_pipeline: '执行求职流程',
  }
  return labels[name] || name
}

function CitationList({ citations }: { citations: Citation[] }) {
  return <details className="assistant-sources mt-4" aria-label="参考资料">
	<summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md px-1.5 py-1 text-xs font-medium text-text-secondary transition hover:bg-surface-raised hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
	  <Globe2 className="h-3.5 w-3.5" aria-hidden="true" />
	  <span>来源</span>
	  <span className="font-normal text-text-tertiary">{citations.length}</span>
	  <ChevronDown className="assistant-sources-chevron h-3.5 w-3.5 text-text-tertiary" aria-hidden="true" />
	</summary>
	<div className="mt-2 divide-y divide-card-border border-y border-card-border">
      {citations.map((citation, index) => {
        const Icon = citationIcon[citation.source]
        const content = <>
          <span className="assistant-citation-index">{index + 1}</span>
		  <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-tertiary" aria-hidden="true" />
		  <span className="min-w-0 flex-1">
			<span className="block truncate text-xs font-medium text-foreground">{citationLabel(citation)}</span>
			{citation.excerpt && <span className="mt-0.5 block line-clamp-2 text-xs leading-4 text-text-tertiary">{citation.excerpt}</span>}
		  </span>
		  {citation.url && <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-text-tertiary" aria-hidden="true" />}
        </>
        return citation.url ? <a
          key={`${citation.label}-${index}`}
          href={citation.url}
          target="_blank"
          rel="noopener noreferrer"
		  title={citation.excerpt || citation.label}
		  className="assistant-citation"
        >{content}</a> : <span
          key={`${citation.label}-${index}`}
          title={citation.excerpt || citation.label}
		  className="assistant-citation"
        >{content}</span>
      })}
    </div>
  </details>
}

function Capability({ icon: Icon, label, active }: { icon: typeof Database; label: string; active: boolean }) {
  return <div className="flex items-center gap-2"><Icon className={`h-3.5 w-3.5 ${active ? 'text-primary' : 'text-text-tertiary'}`} /><span>{label}</span><span className={`ml-auto h-1.5 w-1.5 rounded-full ${active ? 'bg-success' : 'bg-text-tertiary'}`} /></div>
}
