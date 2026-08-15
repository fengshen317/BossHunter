import { useCallback, useEffect, useState } from 'react'

interface FunnelData {
  [key: string]: number
}

interface ActivityData {
  day: string
  action: string
  cnt: number
}

interface Job {
  id: string
  title: string
  company: string
  salary: string
  city: string
  experience: string
  jd: string
  score: number
  score_reason: string
  greeting: string
  status: string
  hr_name: string
  hr_title: string
  company_size: string
  company_industry: string
  district?: string
  company_background?: string
  registered_capital?: string
  url: string
  created_at: string
  updated_at?: string
  resume_path?: string
  last_error?: string
}

interface TopCompany {
  company: string
  avg_score: number
  job_count: number
}

/** /api/jobs 的分页响应：当前页岗位 + 全量统计（不受 status 筛选影响）。 */
interface JobsPage {
  items: Job[]
  /** 当前筛选条件下的岗位总数（分页器用）。 */
  total: number
	/** 全量按状态计数，喂给筛选 pill。后端返回 snake_case `status_counts`。 */
	status_counts: Record<string, number>
	/** 待评分岗位数（status=pending），喂给"批量评分"按钮。后端返回 snake_case `pending_count`。 */
	pending_count: number
	/** 企业信息缺失岗位数，喂给"补全企业"按钮。后端返回 snake_case `missing_company_info_count`。 */
	missing_company_info_count: number
}

interface WorkbenchTask {
  id: string
  mode: 'full' | 'collect' | 'score' | 'reenrich' | 'greeting' | 'monitor' | 'deliver' | 'reply' | 'import'
  label: string
  status: string
  logs: string[]
  error?: string
  deadline_at?: string
  stop_reason?: string
  stop_requested: boolean
}

interface WorkbenchData {
  funnel: FunnelData
  pending_confirmation: Job[]
  needs_greeting: Job[]
  pending_greetings: Job[]
  send_errors: Job[]
  needs_resume: Job[]
  task: WorkbenchTask | null
  last_task: WorkbenchTask | null
  // 全部任务（含并发启动的 reply），供前端轮询短任务的完成/失败结果
  tasks: WorkbenchTask[]
}

interface HistoryDetailPayload {
  schema: string
  hr_question: string
  resume_requested?: boolean
  ai_reply: string
  system_reason?: string
  conversation_tail?: Array<{
    sender: string
    text: string
  }>
}

interface HistoryItem {
  id: number
  job_id: string
  action: string
  detail: string
  detail_payload?: HistoryDetailPayload
  created_at: string
  company: string
  title: string
  resume_path?: string
  resolved?: boolean
}

const emptyWorkbench: WorkbenchData = {
  funnel: {},
  pending_confirmation: [],
  needs_greeting: [],
  pending_greetings: [],
  send_errors: [],
  needs_resume: [],
  task: null,
  last_task: null,
  tasks: [],
}

type DashboardDataScope = 'workbench' | 'jobs' | 'monitor' | 'all'

export function useDashboard(scope: DashboardDataScope = 'all', jobsStatus = 'all') {
  const [workbench, setWorkbench] = useState<WorkbenchData>(emptyWorkbench)
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsTotal, setJobsTotal] = useState(0)
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({})
  const [pendingCount, setPendingCount] = useState(0)
  const [missingCompanyInfoCount, setMissingCompanyInfoCount] = useState(0)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const fetchAll = useCallback(async (showRefreshing = false) => {
    if (showRefreshing) setRefreshing(true)
    try {
      const needsWorkbench = scope === 'all' || scope === 'workbench' || scope === 'jobs'
      const needsJobs = scope === 'all' || scope === 'jobs'
      const needsHistory = scope === 'all' || scope === 'monitor'
      const fetchOptions = { cache: 'no-store' as const }
      // 状态筛选交由后端按页返回；切换筛选会带 ?status= 重新请求。
      const jobsQuery = jobsStatus && jobsStatus !== 'all' ? `?status=${encodeURIComponent(jobsStatus)}` : ''
      const [workbenchRes, jobsRes, historyRes] = await Promise.all([
        needsWorkbench ? fetch('/api/workbench', fetchOptions) : Promise.resolve(null),
        needsJobs ? fetch(`/api/jobs${jobsQuery}`, fetchOptions) : Promise.resolve(null),
        needsHistory ? fetch('/api/history?limit=50&include_unresolved=1', fetchOptions) : Promise.resolve(null),
      ])
      const parseResponse = async (response: Response | null, label: string) => {
        if (!response) return undefined
        if (!response.ok) throw new Error(`${label}刷新失败（${response.status}）`)
        return response.json()
      }
      const [workbenchData, jobsData, historyData] = await Promise.all([
        parseResponse(workbenchRes, '工作台'),
        parseResponse(jobsRes, '岗位池'),
        parseResponse(historyRes, '监测记录'),
      ])

      if (workbenchData !== undefined) setWorkbench(workbenchData)
      if (jobsData !== undefined) {
		// /api/jobs 返回 {items,total,status_counts,pending_count,missing_company_info_count} 分页信封
		const page = jobsData as JobsPage
		setJobs(page.items ?? [])
		setJobsTotal(page.total ?? 0)
		setStatusCounts(page.status_counts ?? {})
		setPendingCount(page.pending_count ?? 0)
		setMissingCompanyInfoCount(page.missing_company_info_count ?? 0)
      }
      if (historyData !== undefined) setHistory(historyData)
      setError('')
    } catch (err) {
      console.error('Failed to fetch dashboard data:', err)
      setError(err instanceof Error ? err.message : '无法读取本地控制台数据')
    } finally {
      setLoading(false)
      if (showRefreshing) setRefreshing(false)
    }
  }, [scope, jobsStatus])

  const startTask = async (mode: 'full' | 'collect' | 'score' | 'reenrich' | 'monitor' | 'deliver') => {
    const res = await fetch('/api/workbench/task', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      throw new Error(data.error || '启动失败')
    }
    await fetchAll()
    return data as WorkbenchTask
  }

  const stopTask = async (taskId: string) => {
    const res = await fetch(`/api/workbench/task/${taskId}/stop`, { method: 'POST' })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      throw new Error(data.error || '停止失败')
    }
    await fetchAll()
    return data as WorkbenchTask
  }

  useEffect(() => {
    fetchAll()
    const interval = setInterval(
      () => void fetchAll(),
      workbench.task || scope === 'jobs' ? 2000 : 30000,
    )
    return () => clearInterval(interval)
  }, [fetchAll, workbench.task?.id])

  const refresh = useCallback(() => fetchAll(true), [fetchAll])

  return {
    workbench,
    jobs,
    jobsTotal,
    statusCounts,
    pendingCount,
    missingCompanyInfoCount,
    history,
    loading,
    refreshing,
    error,
    refresh,
    startTask,
    stopTask,
  }
}

export type { FunnelData, ActivityData, Job, JobsPage, TopCompany, WorkbenchData, WorkbenchTask, HistoryDetailPayload, HistoryItem }
