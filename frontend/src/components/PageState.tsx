/** Shared loading skeleton and load-error states (the app shell's pgLoading/pgError). */

export function LoadingState({ title, noun }: { title: string; noun: string }) {
  return (
    <>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17 }}>{title}</h1>
      </div>
      <div role="status" data-content style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-dim)', marginBottom: 6 }}>
          <span className="spinner" />Loading {noun}…
        </div>
        {[1, 2, 3, 4, 5, 6, 7, 8].map((i) => (
          <div key={i} style={{ height: 30, borderBottom: '1px solid var(--border)', display: 'flex', gap: 16, alignItems: 'center' }}>
            <span style={{ height: 10, width: '28%', background: 'var(--bg-hover)', borderRadius: 3 }} />
            <span style={{ height: 10, width: '16%', background: 'var(--bg-hover)', borderRadius: 3 }} />
            <span style={{ height: 10, width: '10%', background: 'var(--bg-hover)', borderRadius: 3 }} />
          </div>
        ))}
      </div>
    </>
  )
}

export function ErrorState({ title, noun, onRetry }: { title: string; noun: string; onRetry: () => void }) {
  return (
    <>
      <div data-toolbar style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 20px', minHeight: 53, borderBottom: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <h1 style={{ margin: 0, fontSize: 17 }}>{title}</h1>
      </div>
      <div data-content style={{ padding: 20 }}>
        <div className="error-banner" role="alert" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ flex: 1, minWidth: 240 }}>
            <strong>Couldn&apos;t load {noun}.</strong> The Verifyarr server didn&apos;t answer within 10 seconds.
            It may be restarting, or busy with a long job. Your files are not affected.
          </span>
          <button className="btn btn-sm" onClick={onRetry}>Try again</button>
        </div>
      </div>
    </>
  )
}
