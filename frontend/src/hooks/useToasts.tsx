import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'

export type ToastKind = 'ok' | 'info' | 'warn' | 'bad'

interface Toast {
  id: number
  text: string
  kind: ToastKind
  action?: () => void
  actionLabel?: string
}

interface ToastsValue {
  toast: (text: string, opts?: { kind?: ToastKind; action?: () => void; actionLabel?: string }) => void
}

const ToastsContext = createContext<ToastsValue | null>(null)

let nextId = 1

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const toast = useCallback((text: string, opts?: { kind?: ToastKind; action?: () => void; actionLabel?: string }) => {
    const id = nextId++
    const t: Toast = { id, text, kind: opts?.kind ?? 'ok', action: opts?.action, actionLabel: opts?.actionLabel }
    setToasts((prev) => [...prev.slice(-3), t])
    setTimeout(() => dismiss(id), t.action ? 7000 : 4500)
  }, [dismiss])

  return (
    <ToastsContext.Provider value={{ toast }}>
      {children}
      <div aria-live="polite" style={{ position: 'fixed', right: 16, bottom: 16, display: 'flex', flexDirection: 'column', gap: 8, zIndex: 70, width: 360, maxWidth: 'calc(100vw - 32px)' }}>
        {toasts.map((t) => (
          <div key={t.id} className="card" role="status" style={{ padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 10, boxShadow: '0 8px 24px rgba(0,0,0,.45)' }}>
            {t.kind === 'ok' && <span aria-hidden="true" style={{ color: 'var(--green)', fontWeight: 700, width: 14, textAlign: 'center' }}>✓</span>}
            {t.kind === 'info' && <span aria-hidden="true" style={{ color: 'var(--blue)', fontWeight: 700, width: 14, textAlign: 'center' }}>i</span>}
            {t.kind === 'warn' && <span aria-hidden="true" style={{ color: 'var(--yellow)', fontWeight: 700, width: 14, textAlign: 'center' }}>!</span>}
            {t.kind === 'bad' && <span aria-hidden="true" style={{ color: 'var(--red)', fontWeight: 700, width: 14, textAlign: 'center' }}>✕</span>}
            <span style={{ flex: 1, fontSize: 13, lineHeight: 1.4 }}>{t.text}</span>
            {t.action && (
              <button className="btn btn-sm" onClick={() => { dismiss(t.id); t.action!() }}>
                {t.actionLabel}
              </button>
            )}
            <button onClick={() => dismiss(t.id)} aria-label="Dismiss" style={{ background: 'none', border: 0, color: 'var(--text-dim)', fontSize: 17, padding: '0 2px', lineHeight: 1 }}>
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastsContext.Provider>
  )
}

export function useToasts(): ToastsValue {
  const ctx = useContext(ToastsContext)
  if (!ctx) throw new Error('useToasts must be used inside ToastProvider')
  return ctx
}
