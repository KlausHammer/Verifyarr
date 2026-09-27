import { useState } from 'react'
import { LanguageMultiSelect } from 'verifyarr-ui'

// Used inside a settings form: .card > .field > label + control.
const Form = ({ children }: { children: React.ReactNode }) => (
  <div className="card" style={{ minHeight: 120 }}>
    <div className="field">
      <label>Subtitle languages</label>
      {children}
    </div>
  </div>
)

export const Selected = () => {
  const [codes, setCodes] = useState(['en', 'da'])
  return <Form><LanguageMultiSelect codes={codes} onChange={setCodes} /></Form>
}

export const NoneSelected = () => {
  const [codes, setCodes] = useState<string[]>([])
  return <Form><LanguageMultiSelect codes={codes} onChange={setCodes} /></Form>
}
