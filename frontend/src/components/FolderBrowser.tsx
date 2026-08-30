import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import styles from './FolderBrowser.module.css'

interface BrowseEntry {
  name: string
  path: string
}

interface BrowseResponse {
  path: string
  parent: string | null
  entries: BrowseEntry[]
}

export default function FolderBrowser({
  initialPath = '/',
  onSelect,
  onClose,
}: {
  initialPath?: string
  onSelect: (path: string) => void
  onClose: () => void
}) {
  const [data, setData] = useState<BrowseResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  function load(path: string) {
    setLoading(true)
    setError(null)
    api
      .get<BrowseResponse>(`/browse?path=${encodeURIComponent(path)}`)
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load(initialPath)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={`card ${styles.modal}`} onClick={(e) => e.stopPropagation()}>
        <div className={styles.header}>
          <h3 style={{ margin: 0 }}>Choose a folder</h3>
          <button className={styles.closeBtn} onClick={onClose}>
            ✕
          </button>
        </div>

        <div className={styles.pathBar}>
          <button className="btn btn-sm" disabled={!data?.parent} onClick={() => data?.parent && load(data.parent)}>
            ↑ Up
          </button>
          <div className={styles.currentPath}>{data?.path ?? initialPath}</div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className={styles.list}>
          {loading && (
            <div style={{ padding: 14 }} className="text-dim">
              <span className="spinner" /> Loading…
            </div>
          )}
          {!loading && data?.entries.length === 0 && (
            <div style={{ padding: 14 }} className="text-dim">
              No subfolders here.
            </div>
          )}
          {!loading &&
            data?.entries.map((entry) => (
              <button key={entry.path} className={styles.entry} onClick={() => load(entry.path)}>
                <span className={styles.entryIcon}>📁</span>
                {entry.name}
              </button>
            ))}
        </div>

        <div className={styles.footer}>
          <span className="text-faint" style={{ fontSize: 12 }}>
            Navigate to the exact folder you want to add, then confirm.
          </span>
          <button
            className="btn btn-primary"
            disabled={!data}
            onClick={() => data && onSelect(data.path)}
          >
            Use this folder
          </button>
        </div>
      </div>
    </div>
  )
}
