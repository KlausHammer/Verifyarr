import styles from './ConfirmDialog.module.css'

// Small reusable "are you sure" popup — used for actions that are expensive or easy to trigger
// by accident (e.g. Library's Scan/Rescan). Click-outside and Cancel both back out.
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = 'Continue',
  danger,
  onConfirm,
  onCancel,
}: {
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <div className={styles.overlay} onClick={onCancel}>
      <div className={`card ${styles.modal}`} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0, marginBottom: 10 }}>{title}</h3>
        <p className="text-dim" style={{ marginBottom: 20, fontSize: 13.5, lineHeight: 1.5 }}>
          {message}
        </p>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
          <button className="btn btn-sm" onClick={onCancel}>
            Cancel
          </button>
          <button className={`btn btn-sm ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
