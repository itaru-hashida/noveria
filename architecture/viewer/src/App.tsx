import { useCallback, useEffect, useState } from 'react'
import type { Architecture } from './types'
import { loadArchitecture } from './data'
import GraphView from './GraphView'
import SequenceView from './SequenceView'
import OwnershipView from './OwnershipView'

type Tab = 'map' | 'sequence' | 'owners'

const tabs: { id: Tab; title: string }[] = [
  { id: 'map', title: 'Карта экономики' },
  { id: 'sequence', title: 'Месячный ход' },
  { id: 'owners', title: 'Владение и записи' },
]

export default function App() {
  const [state, setState] = useState<'loading' | 'error' | 'ready'>('loading')
  const [error, setError] = useState('')
  const [data, setData] = useState<Architecture | null>(null)
  const [tab, setTab] = useState<Tab>('map')
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    loadArchitecture()
      .then((value) => {
        if (cancelled) return
        setData(value)
        setState('ready')
      })
      .catch((exception: unknown) => {
        if (cancelled) return
        setError(exception instanceof Error ? exception.message : String(exception))
        setState('error')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const openSequence = useCallback((id: string) => {
    setSelected(id)
    setTab('sequence')
  }, [])

  const openOwners = useCallback((id: string) => {
    setSelected(id)
    setTab('owners')
  }, [])

  if (state === 'loading') return <div className="empty">Загружаю architecture.json…</div>
  if (state === 'error' || !data) {
    return (
      <div className="empty">
        <div>
          <p>Не удалось загрузить architecture.json: {error}</p>
          <p className="muted">
            Сгенерируйте файл: <code>python architecture/generate_architecture.py</code>
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>Noveria — архитектура экономики</h1>
        <nav className="tabs">
          {tabs.map((item) => (
            <button
              key={item.id}
              className={item.id === tab ? 'active' : ''}
              onClick={() => setTab(item.id)}
            >
              {item.title}
            </button>
          ))}
        </nav>
        <span className="meta">
          {data.meta.branch} @ {data.meta.commit.slice(0, 7)}
        </span>
        <span className="counter">
          {data.stats.nodes} узлов · {data.stats.edges} связей · {data.stats.violations} нарушений
        </span>
      </header>

      {tab === 'map' && (
        <GraphView
          data={data}
          selected={selected}
          onSelect={setSelected}
          onOpenSequence={openSequence}
          onOpenOwners={openOwners}
        />
      )}
      {tab === 'sequence' && (
        <SequenceView data={data} selected={selected} onSelect={setSelected} onOpenOwners={openOwners} />
      )}
      {tab === 'owners' && (
        <OwnershipView data={data} selected={selected} onSelect={setSelected} onOpenSequence={openSequence} />
      )}
    </div>
  )
}
