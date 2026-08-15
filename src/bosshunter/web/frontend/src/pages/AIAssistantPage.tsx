import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { Bot, Brain, Check, ChevronRight, Clipboard, Database, ExternalLink, Globe2, Loader2, Plus, Send, ShieldCheck, Sparkles, ThumbsDown, ThumbsUp, Trash2, UserRound, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

interface Conversation { id: string; title: string; preview?: string }
interface Job { id: string; title: string; company: string }
interface Citation {
  source: 'database' | 'resume' | 'memory' | 'web' | 'company_research'
  label: string
  url?: string
  excerpt?: string
}
interface ApprovalItem { id: string; title?: string; company?: string; greeting?: string; message?: string; detail?: string }
interface Approval {
  action_id?: string
  type?: 'collect_jobs' | 'score_jobs' | 'complete_job_details' | 'generate_greetings' | 'send_greetings' | 'monitor_replies' | 'run_pipeline' | 'send_hr_reply' | 'reply_draft_approval'
  title: string
  summary?: string
  draft?: string
  affected_items?: ApprovalItem[]
  allowed_actions: string[]
}
interface TaskCard { id: string; mode: string; label: string; status: string; logs: string[]; error?: string; created_at: string }
interface ToolTrace { name: string; status: 'completed' | 'approval_required' | 'started' }
interface PlanStep { id: string; label: string; status: 'pending' | 'approval_required' | 'started' | 'completed' | 'failed' | 'stopped' }
interface ExecutionEvent {
  id: string
  label: string
  status: 'running' | 'completed' | 'failed' | 'waiting'
  detail?: string
}
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  message_type: 'text' | 'reply_draft' | 'approval'
  metadata?: { citations?: Citation[]; approval?: Approval | null; task?: TaskCard | null; tool_trace?: ToolTrace[]; execution_plan?: PlanStep[]; execution_events?: ExecutionEvent[] }
}
interface Memory { id: string; content: string; memory_type: string }

const quickPrompts = [
  '采集 Python Agent 岗位，沿用已配置城市',
  '开始为待处理岗位评分',
  '为已评分岗位生成招呼语',
  '分析我当前的投递情况',
]
const citationIcon = { database: Database, resume: Clipboard, memory: Brain, web: Globe2, company_research: Brain }

async function readJson<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.error || '请求失败')
  return data as T
}

function renderMarkdown(content: string, citations: Citation[] = []): string {
  const withLinks = content.replace(/\[来源\s*(\d+)\]/g, (full, number: string) => {
    const citation = citations[Number(number) - 1]
    return citation?.url ? `[${number}](${citation.url})` : full
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

function citationLabel(citation: Citation): string {
  return citation.label.replace(/^\[\d+\]\s*/, '').trim() || '未命名来源'
}

export default function AIAssistantPage() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobId, setJobId] = useState('')
  const [memories, setMemories] = useState<Memory[]>([])
  // 联网检索固定开启，保证回答能够引用最新公开信息。
  const webEnabled = true
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [editingDraft, setEditingDraft] = useState('')
  const [selectedConversations, setSelectedConversations] = useState<string[]>([])
  const [conversationSelectMode, setConversationSelectMode] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  const activeApproval = useMemo(
    () => messages[messages.length - 1]?.metadata?.approval || null,
    [messages],
  )
  const activeApprovalMessageId = activeApproval ? messages[messages.length - 1]?.id : null
  const loadConversations = useCallback(async () => {
    const data = await readJson<{ items: Conversation[] }>(await fetch('/api/assistant/conversations'))
    setConversations(data.items || [])
    return data.items || []
  }, [])
  const loadMessages = useCallback(async (id: string) => {
    const data = await readJson<{ items: ChatMessage[] }>(await fetch(`/api/assistant/conversations/${id}/messages`))
    setMessages(data.items || [])
  }, [])
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
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: sending ? 'auto' : 'smooth',
    })
  }, [messages, sending])

  const sendMessage = async (text: string) => {
    const content = text.trim()
    if (!content || sending || activeApproval) return
    let targetId = conversationId
    if (!targetId) targetId = await createConversation()
    const optimistic: ChatMessage = { id: `local-${Date.now()}`, role: 'user', content, message_type: 'text' }
    setMessages(current => [...current, optimistic])
    setInput('')
    setError('')
    setSending(true)
    try {
      const response = await fetch(`/api/assistant/conversations/${targetId}/messages/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: content, job_id: jobId || undefined, web_enabled: webEnabled }),
      })
      if (!response.ok || !response.body) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.error || '发送失败')
      }
      const assistantId = `stream-${Date.now()}`
      setMessages(current => [...current, { id: assistantId, role: 'assistant', content: '', message_type: 'text' }])
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
          const payload = JSON.parse(dataLine.slice(6)) as { id?: string; label?: string; detail?: string; delta?: string; citations?: Citation[]; approval?: Approval | null; task?: TaskCard | null; tool_trace?: ToolTrace[]; execution_plan?: PlanStep[]; execution_events?: ExecutionEvent[]; status?: string; error?: string }
          if (event.startsWith('event: delta')) {
            streamedContent += payload.delta || ''
            setMessages(current => current.map(item => item.id === assistantId ? { ...item, content: streamedContent } : item))
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
            setMessages(current => current.map(item => item.id === assistantId ? {
              ...item,
              message_type: payload.approval ? 'approval' : 'text',
              metadata: { citations: payload.citations, approval: payload.approval, task: payload.task, tool_trace: payload.tool_trace, execution_plan: payload.execution_plan, execution_events: payload.execution_events },
            } : item))
          } else if (event.startsWith('event: error')) {
            throw new Error(payload.error || '生成回答失败')
          }
        }
        if (done) break
      }
      await Promise.all([loadMessages(targetId), loadConversations(), loadMemories()])
    } catch (err) {
      setMessages(current => current.filter(item => item.id !== optimistic.id && !item.id.startsWith('stream-')))
      setInput(content)
      setError(err instanceof Error ? err.message : '发送失败')
    } finally {
      setSending(false)
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
        body: JSON.stringify({ action, action_id: approval?.action_id, selected_item_ids: selectedItemIds, edited_reply: editedReply || (action === 'edit' ? editingDraft : undefined) }),
      }))
      setEditingDraft('')
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
    if (!ids.length || !window.confirm(`确定删除 ${ids.length} 个对话吗？`)) return
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
    <div className="assistant-shell -m-6 grid h-[calc(100vh-3.5rem)] max-h-[calc(100vh-3.5rem)] grid-cols-1 overflow-hidden xl:grid-cols-[248px_minmax(0,1fr)_280px]">
      <aside className="hidden border-r border-card-border bg-surface p-4 xl:flex xl:flex-col">
        <div className="mb-5 flex items-center justify-between">
          <div className="text-sm font-semibold text-foreground">求职对话</div>
          <div className="flex items-center gap-1">
            <Button size="icon" variant="ghost" title={conversationSelectMode ? '退出选择模式' : '批量删除对话'} onClick={() => { setConversationSelectMode(mode => !mode); setSelectedConversations([]) }}>{conversationSelectMode ? <X className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}</Button>
            {conversationSelectMode && selectedConversations.length > 0 && <Button size="icon" variant="ghost" title="删除选中对话" onClick={() => deleteConversations(selectedConversations)}><Trash2 className="h-3.5 w-3.5 text-danger" /></Button>}
            <Button size="icon" variant="secondary" title="新建对话" onClick={createConversation}><Plus className="h-4 w-4" /></Button>
          </div>
        </div>
        <div className="space-y-1 overflow-y-auto">
          {conversations.map(item => (
            <div key={item.id} className={`group flex items-start gap-2 rounded-xl border px-2 py-2 transition ${item.id === conversationId ? 'border-primary/30 bg-primary/10' : 'border-transparent hover:border-card-border hover:bg-surface-raised'}`}>
            {conversationSelectMode && <button onClick={() => setSelectedConversations(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} className={`mt-2 h-4 w-4 rounded-full border ${selectedConversations.includes(item.id) ? 'border-primary bg-primary' : 'border-card-border'}`} aria-label={`选择对话 ${item.title}`} />}
            <button onClick={() => conversationSelectMode ? setSelectedConversations(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id]) : (() => { setConversationId(item.id); setError(''); loadMessages(item.id) })()}
              className="min-w-0 flex-1 py-1 text-left">
              <div className="truncate text-xs font-medium text-foreground">{item.title}</div>
              <div className="mt-1 truncate text-[10px] text-text-tertiary">{item.preview || '等待第一条消息'}</div>
            </button><button onClick={() => deleteConversations([item.id])} title="删除对话" className="mt-1 p-1 opacity-0 transition group-hover:opacity-100"><X className="h-3.5 w-3.5 text-text-tertiary hover:text-danger" /></button>
            </div>
          ))}
        </div>
        <div className="mt-auto rounded-xl border border-primary/15 bg-primary/[0.04] p-3">
          <div className="flex items-center gap-2 text-xs font-medium text-primary"><ShieldCheck className="h-3.5 w-3.5" />人工确认已启用</div>
          <p className="mt-2 text-[10px] leading-4 text-text-tertiary">Agent 可以分析和生成草稿，但不会绕过确认执行敏感操作。</p>
        </div>
      </aside>

      <main className="relative flex min-h-0 overflow-hidden flex-col">
        <div className="border-b border-card-border bg-surface px-5 py-4">
          <div className="mx-auto flex max-w-3xl items-center justify-between gap-4">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-primary/30 bg-primary/10"><Bot className="h-5 w-5 text-primary" /></div>
              <div className="min-w-0">
                <h2 className="truncate text-sm font-semibold text-foreground">BossHunter Agent</h2>
                <p className="mt-0.5 text-[11px] text-text-tertiary">岗位数据 · 当前简历 · 长期记忆 · HR 回复</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <div className="hidden rounded-full border border-success/20 bg-success/5 px-2.5 py-1 text-[10px] text-success sm:block">LangGraph 已连接</div>
              <Button size="icon" variant="secondary" className="xl:hidden" onClick={createConversation}><Plus className="h-4 w-4" /></Button>
            </div>
          </div>
        </div>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6">
          <div className="mx-auto max-w-3xl">
            {!messages.length && (
              <div className="py-8 sm:py-14">
                <div className="max-w-xl">
                  <h3 className="text-2xl font-semibold leading-tight text-foreground sm:text-3xl">不只是回答问题，<span className="text-primary">而是理解你的求职进度。</span></h3>
                  <p className="mt-4 max-w-lg text-sm leading-6 text-text-secondary">我可以读取本地岗位数据库和当前简历，延续多轮对话，并在生成 HR 回复后等待你确认。</p>
                </div>
                <div className="mt-8 grid gap-2 sm:grid-cols-2">
                  {quickPrompts.map(prompt => (
                    <button key={prompt} onClick={() => sendMessage(prompt)} className="group flex items-center justify-between rounded-xl border border-card-border bg-surface px-4 py-3 text-left text-xs text-text-secondary transition hover:border-primary/30 hover:text-foreground">
                      <span>{prompt}</span><ChevronRight className="h-3.5 w-3.5 text-text-tertiary transition group-hover:translate-x-0.5 group-hover:text-primary" />
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="space-y-6">
              {messages.map(message => <MessageBubble key={message.id} message={message} conversationId={conversationId} approvalActive={message.id === activeApprovalMessageId} editingDraft={editingDraft} setEditingDraft={setEditingDraft} resolveApproval={resolveApproval} busy={sending} />)}
              {sending && !messages.some(message => message.id.startsWith('stream-')) && <div className="flex items-center gap-3 text-xs text-text-tertiary"><div className="grid h-8 w-8 place-items-center rounded-lg border border-primary/20 bg-primary/5"><Loader2 className="h-3.5 w-3.5 animate-spin text-primary" /></div>正在连接 Agent…</div>}
            </div>
          </div>
        </div>

        <div className="border-t border-card-border bg-surface px-4 py-4 sm:px-6">
          <form onSubmit={handleSubmit} className="mx-auto max-w-3xl">
            {error && <div className="mb-2 rounded-lg border border-danger/25 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}
            {activeApproval && <div className="mb-2 flex items-center gap-2 rounded-lg border border-warning/25 bg-warning/5 px-3 py-2 text-[11px] text-warning"><ShieldCheck className="h-3.5 w-3.5" />请先处理上方待确认的回复草稿，再继续对话。</div>}
            <div className="rounded-2xl border border-card-border-strong bg-surface p-2 shadow-card transition focus-within:border-primary">
              <Textarea value={input} onChange={event => setInput(event.target.value)}
                onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); handleSubmit(event) } }}
                disabled={sending || Boolean(activeApproval)} placeholder={activeApproval ? '请先完成回复确认' : '询问岗位、简历、求职策略，或粘贴 HR 消息…'}
                className="min-h-[68px] resize-none border-0 bg-transparent shadow-none focus:ring-0" />
              <div className="flex items-center justify-between gap-3 px-1 pb-1">
                <div className="flex min-w-0 items-center gap-2">
                  <Select value={jobId} onChange={event => setJobId(event.target.value)} className="h-8 max-w-[210px] border-0 bg-transparent text-[11px]">
                    <option value="">自动选择岗位上下文</option>
                    {jobs.map(job => <option key={job.id} value={job.id}>{job.company} · {job.title}</option>)}
                  </Select>
                </div>
                <Button size="icon" disabled={!input.trim() || sending || Boolean(activeApproval)} title="发送"><Send className="h-4 w-4" /></Button>
              </div>
            </div>
            <p className="mt-2 text-center text-[10px] text-text-tertiary">Agent 可能出错；涉及个人经历与 HR 回复时请核对事实。</p>
          </form>
        </div>
      </main>

      <aside className="hidden border-l border-card-border bg-surface p-4 xl:block">
        <div className="mb-6">
          <div className="flex items-center gap-2 text-xs font-semibold text-foreground"><Brain className="h-3.5 w-3.5 text-primary" />长期记忆<span className="ml-auto rounded-full bg-surface-raised px-2 py-0.5 text-[9px] text-text-tertiary">{memories.length}</span></div>
          <p className="mt-2 text-[10px] leading-4 text-text-tertiary">只有明确说“记住……”的内容才会自动保存，你可以随时删除。</p>
        </div>
        <div className="space-y-2">
          {!memories.length && <div className="rounded-xl border border-dashed border-card-border p-4 text-center text-[10px] text-text-tertiary">暂无长期记忆</div>}
          {memories.map(memory => (
            <div key={memory.id} className="group rounded-xl border border-card-border bg-surface-raised p-3">
              <div className="flex items-start gap-2"><div className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" /><p className="flex-1 text-[11px] leading-5 text-muted">{memory.content}</p>
                <button onClick={() => forgetMemory(memory.id)} className="opacity-0 transition group-hover:opacity-100" title="删除记忆"><Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-danger" /></button>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-8 border-t border-card-border pt-5">
          <div className="mb-3 text-xs font-semibold text-foreground">本轮能力</div>
          <div className="space-y-2 text-[11px] text-muted">
            <Capability icon={Database} label="SQLite 岗位数据" active />
            <Capability icon={Clipboard} label="当前简历" active />
            <Capability icon={Brain} label="跨会话记忆" active />
            <Capability icon={Globe2} label="联网检索" active={webEnabled} />
          </div>
        </div>
      </aside>
    </div>
  )
}

function MessageBubble({ message, conversationId, approvalActive, editingDraft, setEditingDraft, resolveApproval, busy }: {
  message: ChatMessage; editingDraft: string; setEditingDraft: (value: string) => void
  conversationId: string
  approvalActive: boolean
  resolveApproval: (action: 'approve' | 'edit' | 'reject', approval?: Approval, selectedItemIds?: string[], editedReply?: string) => void; busy: boolean
}) {
  const isUser = message.role === 'user'
  const approval = message.metadata?.approval
  const task = message.metadata?.task
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(message.content)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && <div className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-primary/20 bg-primary/5"><Bot className="h-3.5 w-3.5 text-primary" /></div>}
      <div className={`min-w-0 ${isUser ? 'max-w-[82%]' : 'max-w-[88%]'}`}>
        <div className={`text-sm leading-6 ${isUser ? 'rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-white' : 'text-foreground'}`}>
          {isUser ? message.content : <div className="assistant-markdown" dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content, message.metadata?.citations) }} />}
        </div>
		{!isUser && message.message_type === 'text' && !task && !approval && message.metadata?.citations && message.metadata.citations.length > 0 && <CitationList citations={message.metadata.citations} />}
        {!isUser && message.metadata?.execution_events && message.metadata.execution_events.length > 0 && <ExecutionTraceCard events={message.metadata.execution_events} />}
        {!isUser && message.metadata?.tool_trace && message.metadata.tool_trace.length > 0 && <ToolTraceCard traces={message.metadata.tool_trace} />}
        {!isUser && message.metadata?.execution_plan && message.metadata.execution_plan.length > 0 && <ExecutionPlanCard steps={message.metadata.execution_plan} task={task} />}
        {approval && (
          <div className={`mt-4 overflow-hidden rounded-2xl border ${approvalActive ? 'border-warning/30 bg-warning/5' : 'border-card-border bg-surface opacity-70'}`}>
            <div className={`flex items-center gap-2 border-b px-4 py-3 text-xs font-medium ${approvalActive ? 'border-warning/20 text-warning' : 'border-card-border text-text-tertiary'}`}><ShieldCheck className="h-4 w-4" />{approval.title}<span className="ml-auto text-[10px] font-normal">{approvalActive ? '等待审批' : '已处理'}</span></div>
            <div className="p-4">
              <ApprovalCard approval={approval} active={approvalActive} editingDraft={editingDraft} setEditingDraft={setEditingDraft} resolveApproval={resolveApproval} busy={busy} />
            </div>
          </div>
        )}
        {task && <AssistantTaskCard initialTask={task} />}
        {!isUser && <MessageFeedback conversationId={conversationId} message={message} onCopy={!approval ? copy : undefined} copied={copied} />}
      </div>
      {isUser && <div className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-card-border bg-surface-raised"><UserRound className="h-3.5 w-3.5 text-muted" /></div>}
    </div>
  )
}

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
  return <div className="mt-2">
    <div className="flex items-center gap-1" aria-label="评价这条回答">
      {onCopy && <button onClick={onCopy} className="mr-1 inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[10px] text-text-tertiary hover:bg-surface-raised hover:text-primary">{copied ? <Check className="h-3 w-3" /> : <Clipboard className="h-3 w-3" />}{copied ? '已复制' : '复制'}</button>}
      <button type="button" onClick={() => rate('helpful')} disabled={disabled} aria-label="这条回答有帮助" aria-pressed={rating === 'helpful'} title="有帮助" className={`rounded-md p-1.5 transition ${rating === 'helpful' ? 'bg-success/10 text-success' : 'text-text-tertiary hover:bg-surface-raised hover:text-foreground'} disabled:cursor-wait disabled:opacity-40`}><ThumbsUp className="h-3.5 w-3.5" /></button>
      <button type="button" onClick={() => rate('unhelpful')} disabled={disabled} aria-label="这条回答有问题" aria-pressed={rating === 'unhelpful'} title="有问题" className={`rounded-md p-1.5 transition ${rating === 'unhelpful' ? 'bg-danger/10 text-danger' : 'text-text-tertiary hover:bg-surface-raised hover:text-foreground'} disabled:cursor-wait disabled:opacity-40`}><ThumbsDown className="h-3.5 w-3.5" /></button>
      {rating === 'helpful' && <span className="ml-1 text-[10px] text-success">感谢反馈</span>}
      {submitted && <span className="ml-1 text-[10px] text-success">已加入 Bad Case</span>}
    </div>
    {showForm && !submitted && <div className="mt-2 max-w-xl rounded-xl border border-card-border bg-surface p-3">
      <div className="mb-3">
        <p className="text-xs font-medium text-foreground">这条回答哪里有问题？</p>
        <p className="mt-1 text-[10px] leading-4 text-text-tertiary">系统会自动附带原问题、识别意图和路由轨迹。</p>
      </div>
      <label className="block text-[10px] font-medium text-text-secondary">问题类型
        <Select value={category} onChange={event => setCategory(event.target.value)} className="mt-1 h-9 text-xs">
          {badCaseCategories.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </Select>
      </label>
      <label className="mt-3 block text-[10px] font-medium text-text-secondary">你期望它理解成什么？（选填）
        <input value={expectedIntent} onChange={event => setExpectedIntent(event.target.value)} maxLength={120} placeholder="例如：只查询岗位，不要启动采集" className="mt-1 h-9 w-full rounded-lg border border-card-border bg-surface px-3 text-xs text-foreground outline-none placeholder:text-text-tertiary focus:border-primary" />
      </label>
      <label className="mt-3 block text-[10px] font-medium text-text-secondary">正确回答或补充说明（选填）
        <Textarea value={feedback} onChange={event => setFeedback(event.target.value)} maxLength={2000} placeholder="告诉我们正确的处理方式" className="mt-1 min-h-[72px] text-xs" />
      </label>
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" onClick={submitBadCase} disabled={submitting}>{submitting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}提交问题</Button>
        <Button size="sm" variant="ghost" onClick={() => setShowForm(false)} disabled={submitting}>暂不补充</Button>
      </div>
    </div>}
    {feedbackError && <p className="mt-1 text-[10px] text-danger">{feedbackError}</p>}
  </div>
}

function ApprovalCard({ approval, active, editingDraft, setEditingDraft, resolveApproval, busy }: {
  approval: Approval
  active: boolean
  editingDraft: string
  setEditingDraft: (value: string) => void
  resolveApproval: (action: 'approve' | 'edit' | 'reject', approval?: Approval, selectedItemIds?: string[], editedReply?: string) => void
  busy: boolean
}) {
  const selectable = approval.type === 'send_greetings' || approval.type === 'send_hr_reply'
  const items = approval.affected_items || []
  const [selected, setSelected] = useState<string[]>([])
  const reply = editingDraft || approval.affected_items?.[0]?.message || approval.draft || ''
  const toggle = (id: string) => setSelected(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id])
  return <>
    {approval.summary && <p className="mb-3 text-xs leading-5 text-muted">{approval.summary}</p>}
    {items.map(item => {
      const id = approval.type === 'send_hr_reply' ? item.id : item.id
      return <label key={id} className="mb-2 flex gap-2 rounded-lg border border-card-border bg-surface p-3 text-xs">
        {selectable && <input type="checkbox" checked={selected.includes(id)} onChange={() => toggle(id)} disabled={!active} className="mt-0.5 disabled:cursor-not-allowed" />}
        <span className="min-w-0"><span className="font-medium text-foreground">{item.company} · {item.title}</span>{item.greeting && <span className="mt-1 block whitespace-pre-wrap text-muted">{item.greeting}</span>}{item.detail && <span className="mt-1 block text-muted">{item.detail}</span>}</span>
      </label>
    })}
    {approval.type === 'send_hr_reply' || approval.type === 'reply_draft_approval' ? <Textarea value={reply} onChange={event => setEditingDraft(event.target.value)} disabled={!active} className="min-h-[110px] bg-surface text-sm leading-6 disabled:cursor-not-allowed disabled:opacity-60" /> : null}
    <div className="mt-3 flex flex-wrap gap-2">
      <Button size="sm" onClick={() => resolveApproval('approve', approval, selectable ? selected : [], reply)} disabled={!active || busy || (selectable && selected.length === 0)} className="disabled:cursor-not-allowed disabled:opacity-40"><Check className="mr-1.5 h-3.5 w-3.5" />{active ? '确认并执行' : '已处理'}</Button>
      <Button size="sm" variant="ghost" onClick={() => resolveApproval('reject', approval)} disabled={!active || busy} className="disabled:cursor-not-allowed disabled:opacity-40">取消</Button>
    </div>
    <p className="mt-3 text-[10px] text-text-tertiary">确认后才会启动任务；发送仍受频率、窗口和平台安全校验约束。</p>
  </>
}

function ToolTraceCard({ traces }: { traces: ToolTrace[] }) {
  const labels: Record<string, string> = {
    job_database: '岗位数据库查询',
    web_search: '联网搜索',
    company_research: '公司背调子 Agent',
    collect_jobs: '岗位采集任务',
    score_jobs: 'AI 评分任务',
    complete_job_details: '补充岗位信息并评分',
    generate_greetings: '招呼语生成任务',
    send_greetings: '招呼语发送任务',
    send_hr_reply: 'HR 回复发送任务',
    monitor_replies: 'HR 回复监测任务',
    run_pipeline: '完整求职流程',
  }
  const statusLabels: Record<ToolTrace['status'], string> = { completed: '已完成', approval_required: '等待确认', started: '已启动' }
  return <div className="mt-3 flex flex-wrap gap-1.5">
    {traces.map((trace, index) => <span key={`${trace.name}-${index}`} className="assistant-tool-trace border-primary/20 bg-primary/[0.04] text-primary">
      <Sparkles className="h-3 w-3 shrink-0" />{labels[trace.name] || trace.name} · {statusLabels[trace.status] || trace.status}
    </span>)}
  </div>
}

function CitationList({ citations }: { citations: Citation[] }) {
  return <section className="mt-4" aria-label="参考资料">
    <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-foreground">
      <span>参考资料</span><span className="font-normal tracking-normal">{citations.length} 条</span>
    </div>
    <div className="flex flex-wrap gap-2">
      {citations.map((citation, index) => {
        const Icon = citationIcon[citation.source]
        const content = <>
          <span className="assistant-citation-index">{index + 1}</span>
          <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{citationLabel(citation)}</span>
          {citation.url && <ExternalLink className="h-3 w-3 shrink-0 text-text-tertiary" aria-hidden="true" />}
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
  </section>
}

function ExecutionTraceCard({ events }: { events: ExecutionEvent[] }) {
  // 兼容修复前已落库的历史消息：同一步骤可能同时保存 running/completed，展示时只取最终状态。
  const visibleEvents = normalizeExecutionEvents(events)
  const completed = visibleEvents.filter(event => event.status === 'completed').length
  const active = visibleEvents.some(event => event.status === 'running')
  const statusLabel: Record<ExecutionEvent['status'], string> = {
    running: '执行中', completed: '已完成', failed: '需调整', waiting: '等待输入',
  }
  return <section className="mt-4 rounded-xl border border-card-border bg-surface p-4" aria-label="Agent 执行过程" aria-live="polite">
    <div className="mb-3 flex items-center justify-between gap-3">
      <h4 className="flex items-center gap-2 text-xs font-semibold text-foreground">
        <Sparkles className="h-3.5 w-3.5 text-primary" />执行过程
      </h4>
      <span className="text-[10px] text-text-tertiary">{active ? '处理中' : `${completed}/${visibleEvents.length} 已完成`}</span>
    </div>
    <ol className="space-y-2.5">
      {visibleEvents.map(event => <li key={event.id} className="flex items-start gap-3 text-[11px]">
        <span className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border ${event.status === 'completed' ? 'border-success/40 bg-success/10 text-success' : event.status === 'running' ? 'border-primary/50 bg-primary/10 text-primary' : event.status === 'failed' ? 'border-danger/40 bg-danger/10 text-danger' : 'border-warning/30 bg-warning/10 text-warning'}`}>
          {event.status === 'completed' ? <Check className="h-3 w-3" /> : event.status === 'running' ? <Loader2 className="h-3 w-3 animate-spin" /> : event.status === 'failed' ? <X className="h-3 w-3" /> : <ShieldCheck className="h-3 w-3" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-foreground">{event.label}</span>
          {event.detail && <span className="mt-0.5 block truncate text-[10px] text-text-tertiary">{event.detail}</span>}
        </span>
        <span className="shrink-0 text-[10px] text-text-tertiary">{statusLabel[event.status]}</span>
      </li>)}
    </ol>
  </section>
}

function ExecutionPlanCard({ steps, task }: { steps: PlanStep[]; task?: TaskCard | null }) {
	const [taskStatus, setTaskStatus] = useState(task?.status)
	useEffect(() => {
		if (!task || ['completed', 'failed', 'stopped'].includes(taskStatus || '')) return
		const timer = window.setInterval(() => {
			fetch(`/api/workbench/task/${task.id}`)
				.then(response => response.ok ? response.json() : null)
				.then(next => { if (next?.status) setTaskStatus(next.status) })
				.catch(() => undefined)
		}, 1500)
		return () => window.clearInterval(timer)
	}, [task, taskStatus])
  const visibleSteps = steps.map(step => {
    if (step.id !== 'execute' || !taskStatus) return step
    if (taskStatus === 'running' || taskStatus === 'stopping') return { ...step, status: 'started' as const }
    if (taskStatus === 'completed') return { ...step, status: 'completed' as const }
    if (taskStatus === 'failed') return { ...step, status: 'failed' as const }
    if (taskStatus === 'stopped') return { ...step, status: 'stopped' as const }
    return step
  })
  const statusLabel: Record<PlanStep['status'], string> = {
    pending: '待执行', approval_required: '等待审批', started: '执行中', completed: '已完成', failed: '失败', stopped: '已停止',
  }
  return <section className="mt-4 rounded-xl border border-card-border bg-surface p-4" aria-label="执行规划">
    <div className="mb-3 flex items-center justify-between gap-3">
      <h4 className="text-xs font-semibold text-foreground">执行规划</h4>
      <span className="text-[10px] text-text-tertiary">{visibleSteps.filter(step => step.status === 'completed').length}/{visibleSteps.length} 已完成</span>
    </div>
    <ol className="space-y-3">
      {visibleSteps.map((step, index) => {
        const active = step.status === 'started' || step.status === 'approval_required'
        const done = step.status === 'completed'
        const failed = step.status === 'failed' || step.status === 'stopped'
        return <li key={step.id} className="flex items-start gap-3">
          <span className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border text-[10px] ${done ? 'border-success/40 bg-success/10 text-success' : active ? 'border-primary/50 bg-primary/10 text-primary' : failed ? 'border-danger/40 bg-danger/10 text-danger' : 'border-card-border text-text-tertiary'}`}>
            {done ? <Check className="h-3 w-3" /> : active ? <Loader2 className={`h-3 w-3 ${step.status === 'started' ? 'animate-spin' : ''}`} /> : index + 1}
          </span>
          <span className="min-w-0 flex-1 text-xs leading-5 text-muted">{step.label}</span>
          <span className={`shrink-0 text-[10px] ${active ? 'text-primary' : failed ? 'text-danger' : 'text-text-tertiary'}`}>{statusLabel[step.status]}</span>
        </li>
      })}
    </ol>
  </section>
}

function AssistantTaskCard({ initialTask }: { initialTask: TaskCard }) {
  const [task, setTask] = useState(initialTask)
  useEffect(() => {
    if (['completed', 'failed', 'stopped'].includes(task.status)) return
    const timer = window.setInterval(() => {
      fetch(`/api/workbench/task/${task.id}`).then(response => response.ok ? response.json() : null).then(next => { if (next) setTask(next) }).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task.id, task.status])
  const stop = async () => {
    const response = await fetch(`/api/workbench/task/${task.id}/stop`, { method: 'POST' })
    if (response.ok) setTask(await response.json())
  }
  return <div className="mt-4 rounded-xl border border-primary/20 bg-primary/[0.04] p-3 text-xs">
    <div className="flex items-center gap-2"><Loader2 className={`h-3.5 w-3.5 text-primary ${['running', 'stopping'].includes(task.status) ? 'animate-spin' : ''}`} /><span className="font-medium text-foreground">{task.label}</span><span className="ml-auto text-text-tertiary">{task.status}</span></div>
    {task.logs.slice(-3).map((log, index) => <p key={`${index}-${log}`} className="mt-1 text-text-tertiary">{log}</p>)}
    {task.error && <p className="mt-1 text-danger">{task.error}</p>}
    {['running', 'stopping'].includes(task.status) && <Button size="sm" variant="ghost" className="mt-2" onClick={stop}>停止任务</Button>}
  </div>
}

function Capability({ icon: Icon, label, active }: { icon: typeof Database; label: string; active: boolean }) {
  return <div className="flex items-center gap-2"><Icon className={`h-3.5 w-3.5 ${active ? 'text-primary' : 'text-text-tertiary'}`} /><span>{label}</span><span className={`ml-auto h-1.5 w-1.5 rounded-full ${active ? 'bg-success' : 'bg-text-tertiary'}`} /></div>
}
