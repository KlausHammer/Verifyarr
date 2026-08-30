import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { useAuth } from './hooks/useAuth'
import Login from './routes/Login'
import Setup from './routes/Setup'
import MediaLibrary from './routes/MediaLibrary'
import Files from './routes/Files'
import FileDetail from './routes/FileDetail'
import Activity from './routes/Activity'
import ActivityDetail from './routes/ActivityDetail'
import Stats from './routes/Stats'
import Quarantine from './routes/Quarantine'
import BazarrBlacklist from './routes/BazarrBlacklist'
import Settings from './routes/Settings'

export default function App() {
  const { status, loading } = useAuth()

  if (loading) {
    return (
      <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center' }}>
        <span className="spinner" />
      </div>
    )
  }

  if (status?.needs_setup) {
    return (
      <Routes>
        <Route path="*" element={<Setup />} />
      </Routes>
    )
  }

  if (!status?.authenticated) {
    return (
      <Routes>
        <Route path="*" element={<Login />} />
      </Routes>
    )
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/stats" replace />} />
        <Route path="movies" element={<MediaLibrary kind="movie" title="Movies" folderHint="Movies" />} />
        <Route path="series" element={<MediaLibrary kind="series" title="Series" folderHint="Series" />} />
        <Route path="files" element={<Files />} />
        <Route path="files/:id" element={<FileDetail />} />
        <Route path="activity" element={<Activity />} />
        <Route path="activity/:runId" element={<ActivityDetail />} />
        <Route path="stats" element={<Stats />} />
        <Route path="quarantine" element={<Quarantine />} />
        <Route path="bazarr-blacklist" element={<BazarrBlacklist />} />
        <Route path="settings/:tab" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
