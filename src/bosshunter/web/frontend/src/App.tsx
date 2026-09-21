import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { Sidebar } from './components/layout/Sidebar'
import { Header } from './components/layout/Header'
import DashboardPage from './pages/DashboardPage'
import ConfigPage from './pages/ConfigPage'
import AIAssistantPage from './pages/AIAssistantPage'
import { ConfirmProvider } from './components/confirm-dialog'

function JobsPage() {
	return <DashboardPage view="jobs" />
}

function MonitorPage() {
	return <DashboardPage view="monitor" />
}

function AppLayout() {
	const location = useLocation()
	const assistantActive = location.pathname === '/assistant'

	return (
		<div className="flex h-screen overflow-hidden bg-background text-foreground">
			<Sidebar />
			<div className="flex min-w-0 flex-1 flex-col overflow-hidden">
				<Header />
				<main className={`min-h-0 flex-1 ${assistantActive ? 'overflow-hidden' : 'overflow-y-auto p-4 sm:p-6'}`}>
					<Routes>
						<Route path="/" element={<DashboardPage />} />
						<Route path="/jobs" element={<JobsPage />} />
						<Route path="/monitor" element={<MonitorPage />} />
						<Route path="/assistant" element={<AIAssistantPage />} />
						<Route path="/config" element={<ConfigPage />} />
						<Route path="*" element={<Navigate to="/" replace />} />
					</Routes>
				</main>
			</div>
		</div>
	)
}

export default function App() {
	return <BrowserRouter><ConfirmProvider><AppLayout /></ConfirmProvider></BrowserRouter>
}
