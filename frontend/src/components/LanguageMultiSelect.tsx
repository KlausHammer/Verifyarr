import { useState } from 'react'
import { LANGUAGES, languageName } from '../lib/languages'

export default function LanguageMultiSelect({ codes, onChange }: { codes: string[]; onChange: (codes: string[]) => void }) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)

  const filtered = LANGUAGES.filter(
    (l) => !codes.includes(l.code) &&
      (l.name.toLowerCase().includes(query.toLowerCase()) || l.code.startsWith(query.toLowerCase()))
  ).slice(0, 8)

  function add(code: string) {
    onChange([...codes, code])
    setQuery('')
  }
  function remove(code: string) {
    onChange(codes.filter((c) => c !== code))
  }

  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
        {codes.length === 0 && (
          <span className="text-faint" style={{ fontSize: 12.5 }}>None selected — all languages allowed.</span>
        )}
        {codes.map((code) => (
          <span key={code} className="pill pill-muted" style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            {languageName(code)}
            <button
              type="button"
              onClick={() => remove(code)}
              style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: 'inherit', lineHeight: 1 }}
            >
              ✕
            </button>
          </span>
        ))}
      </div>
      <div style={{ position: 'relative' }}>
        <input
          type="text"
          placeholder="Search language…"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
        {open && query && filtered.length > 0 && (
          <div
            style={{
              position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 10, marginTop: 4,
              background: 'var(--bg-elevated, var(--bg))', border: '1px solid var(--border)',
              borderRadius: 6, maxHeight: 220, overflowY: 'auto',
            }}
          >
            {filtered.map((l) => (
              <div
                key={l.code}
                onMouseDown={() => add(l.code)}
                style={{ padding: '7px 10px', fontSize: 13, cursor: 'pointer' }}
                className="lang-option"
              >
                {l.name} <span className="text-faint">({l.code})</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
