import { ConfirmDialog } from 'verifyarr-ui'

// The dialog is a full-window overlay; the transform makes this box its window.
const Screen = ({ children }: { children: React.ReactNode }) => (
  <div style={{ position: 'relative', height: 300, transform: 'translateZ(0)', background: 'var(--bg)' }}>{children}</div>
)

export const Rescan = () => (
  <Screen>
    <ConfirmDialog
      title="Rescan the whole library?"
      message="Every subtitle is checked again against its audio. On a large library this can take several hours."
      confirmLabel="Rescan"
      onConfirm={() => {}}
      onCancel={() => {}}
    />
  </Screen>
)

export const Delete = () => (
  <Screen>
    <ConfirmDialog
      title="Delete 3 quarantined subtitles?"
      message="The files are removed from disk. Bazarr will search for replacements on its next run."
      confirmLabel="Delete"
      danger
      onConfirm={() => {}}
      onCancel={() => {}}
    />
  </Screen>
)
