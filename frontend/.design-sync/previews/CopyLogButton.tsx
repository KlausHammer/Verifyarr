import { CopyLogButton } from 'verifyarr-ui'

const lines = [
  { ts: '2026-09-27T18:56:49Z', level: 'INFO', message: 'S02E11.en.srt: pooled anchors show a global drift signature' },
  { ts: '2026-09-27T18:57:02Z', level: 'INFO', message: 'S02E11.en.srt: fixed (framerate 23.976 -> 24, up to 1.3s)' },
]

// Sits in the header of a log panel.
const Panel = ({ children }: { children: React.ReactNode }) => (
  <div className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
    <h3 style={{ margin: 0 }}>Recent log</h3>
    {children}
  </div>
)

export const WithLog = () => <Panel><CopyLogButton lines={lines} /></Panel>
export const NoLog = () => <Panel><CopyLogButton lines={[]} /></Panel>
