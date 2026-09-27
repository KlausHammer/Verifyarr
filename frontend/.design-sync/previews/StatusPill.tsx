import { StatusPill } from 'verifyarr-ui'

// Pills sit on the app's dark surfaces (tables, cards), never on white.
const Row = ({ children }: { children: React.ReactNode }) => (
  <div className="card" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>{children}</div>
)

export const Healthy = () => (
  <Row>
    <StatusPill value="ok" />
    <StatusPill value="already in sync" />
    <StatusPill value="completed" />
  </Row>
)
export const Fixed = () => (
  <Row>
    <StatusPill value="fixed (framerate 23.976 -> 24, up to 1.3s)" />
  </Row>
)
export const Problems = () => (
  <Row>
    <StatusPill value="SUSPECT" />
    <StatusPill value="failed" />
    <StatusPill value="missing" />
  </Row>
)
export const InProgress = () => (
  <Row>
    <StatusPill value="running" />
    <StatusPill value="generated" />
    <StatusPill value="would rescale 24 -> 23.976" />
  </Row>
)
export const Undecided = () => (
  <Row>
    <StatusPill value="unknown" />
    <StatusPill value="cancelled" />
    <StatusPill value="skipped" />
    <StatusPill value={null} />
  </Row>
)
