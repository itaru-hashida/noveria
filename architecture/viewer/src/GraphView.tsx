import { useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Architecture, ArchEdge, SourceLocation } from './types'
import { applyVisibility, createGraph, focusNode } from './graph'
import { emptyFilters, ownerNodeIds, visibleNodeIds } from './filters'
import { sourceHref } from './data'

interface Props {
  data: Architecture
  selected: string | null
  onSelect: (id: string | null) => void
  onOpenSequence: (id: string) => void
  onOpenOwners: (id: string) => void
}

export default function GraphView({
  data,
  selected,
  onSelect,
  onOpenSequence,
  onOpenOwners,
}: Props) {
  const canvas = useRef<HTMLDivElement>(null)
  const cyRef = useRef<cytoscape.Core | null>(null)
  const onSelectRef = useRef(onSelect)
  const [query, setQuery] = useState('')
  const [domain, setDomain] = useState('')
  const [languages, setLanguages] = useState<Set<string>>(new Set())
  const [ownersOnly, setOwnersOnly] = useState(false)
  const [writesOnly, setWritesOnly] = useState(false)
  const [callbacksOnly, setCallbacksOnly] = useState(false)
  const [showIntro, setShowIntro] = useState(true)

  onSelectRef.current = onSelect

  const locations = useMemo(() => {
    const map = new Map<string, SourceLocation>()
    for (const item of data.source_locations) map.set(item.id, item)
    return map
  }, [data])

  const active = selected ? data.nodes.find((node) => node.id === selected) ?? null : null

  const visible = useMemo(
    () =>
      visibleNodeIds(data, {
        ...emptyFilters,
        query,
        domain,
        languages,
        ownersOnly,
        writesOnly,
        callbacksOnly,
      }),
    [data, query, domain, languages, ownersOnly, writesOnly, callbacksOnly],
  )

  const visibleEdges = useMemo(() => {
    let count = 0
    for (const edge of data.edges) {
      if (visible.has(edge.source) && visible.has(edge.target)) count += 1
    }
    return count
  }, [data.edges, visible])

  // Граф создаётся ровно один раз на контейнер: повторная инициализация
  // Cytoscape на том же DOM-узле ломает его внутреннее состояние.
  useEffect(() => {
    if (!canvas.current || cyRef.current) return
    const cy = createGraph(canvas.current, data)
    cyRef.current = cy
    cy.on('tap', 'node', (event) => {
      onSelectRef.current(event.target.id() as string)
    })
    cy.on('tap', (event) => {
      if (event.target === cy) onSelectRef.current(null)
    })
    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [data])

  // Фильтр не пересобирает элементы, а скрывает их классом `filtered`:
  // удаление и повторное добавление рёбер в Cytoscape падает на «ребро без
  // источника», поэтому состав графа постоянен, меняется только видимость.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    applyVisibility(cy, visible)
  }, [data, visible])

  // Фокус: выбранный узел и его соседи остаются яркими, остальное гаснет.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    focusNode(cy, selected)
    if (selected) {
      const node = cy.getElementById(selected)
      if (node.nonempty() && !node.hasClass('filtered')) {
        cy.stop()
        cy.animate({ center: { eles: node } }, { duration: 220 })
      }
    }
  }, [selected])

  const toggleLanguage = (value: string) => {
    const next = new Set(languages)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    setLanguages(next)
  }

  const owners = useMemo(() => ownerNodeIds(data), [data])

  const edgeList = (edges: ArchEdge[], direction: 'in' | 'out') => (
    <ul className="tight">
      {edges.map((edge) => {
        const otherId = direction === 'in' ? edge.source : edge.target
        const other = data.nodes.find((node) => node.id === otherId)
        return (
          <li key={edge.id}>
            <button className="loc" onClick={() => onSelect(otherId)}>
              {other?.short ?? otherId}
            </button>{' '}
            <span className={`chip edge-${edge.kind}`}>{edge.kind_title}</span>
            <div className="muted small">{edge.plain}</div>
          </li>
        )
      })}
    </ul>
  )

  const incoming = active ? data.edges.filter((edge) => edge.target === active.id) : []
  const outgoing = active ? data.edges.filter((edge) => edge.source === active.id) : []

  return (
    <>
      <div className="toolbar">
        <input
          className="grow"
          type="search"
          placeholder="Поиск: класс, функция, файл, строка…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <select value={domain} onChange={(event) => setDomain(event.target.value)}>
          <option value="">Все домены</option>
          {data.domains.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
            </option>
          ))}
        </select>
        <button
          className={languages.has('python') ? 'active' : ''}
          onClick={() => toggleLanguage('python')}
          title="Только код на Python"
        >
          Python
        </button>
        <button
          className={languages.has('cython') ? 'active' : ''}
          onClick={() => toggleLanguage('cython')}
          title="Только код на Cython"
        >
          Cython
        </button>
        <button
          className={ownersOnly ? 'active' : ''}
          onClick={() => setOwnersOnly(!ownersOnly)}
          title="Показать только хранилища, которые владеют состоянием"
        >
          Владельцы
        </button>
        <button
          className={writesOnly ? 'active' : ''}
          onClick={() => setWritesOnly(!writesOnly)}
          title="Показать только места, где пишут мимо владельца"
        >
          Прямые записи
        </button>
        <button
          className={callbacksOnly ? 'active' : ''}
          onClick={() => setCallbacksOnly(!callbacksOnly)}
          title="Показать только обратные вызовы из быстрого кода"
        >
          Callbacks
        </button>
        <span className="counter">
          {visible.size} / {data.nodes.length} узлов · {visibleEdges} / {data.edges.length} связей
        </span>
      </div>

      <div className="body">
        <div className="graph-wrap">
          <div id="cy" ref={canvas} />
          <div className="edge-legend">
            {data.legend.edge_kinds.map((item) => (
              <span key={item.id} className={`legend-line edge-${item.id}`} title={item.explanation}>
                {item.title}
              </span>
            ))}
          </div>
          {showIntro && (
            <div className="intro-panel">
              <div className="intro-head">
                <strong>Как читать карту</strong>
                <button onClick={() => setShowIntro(false)} aria-label="Скрыть подсказку">
                  ✕
                </button>
              </div>
              <ol className="intro-list">
                {data.legend.how_to_read.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ol>
              <p className="muted small">
                Колесо — масштаб, перетаскивание — сдвиг. Клик по фигуре открывает справа, что это
                за штука, где она лежит и с чем связана.
              </p>
              <button className="intro-close" onClick={() => setShowIntro(false)}>
                Понятно, скрыть
              </button>
            </div>
          )}
        </div>

        <aside className="side">
          {!active ? (
            <>
              <h2>Что это за схема</h2>
              <p className="muted">
                Каждая фигура — кусочек кода экономики. Стрелка между ними — кто кого зовёт или
                кто что меняет. Чем фигура крупнее и ярче, тем больше в ней сосредоточено.
              </p>

              <h3>Типы фигур</h3>
              <dl className="kv legend-kv">
                {data.legend.node_types.map((item) => (
                  <div key={item.id} className="legend-row">
                    <dt>
                      <span className={`shape-glyph shape-${item.id}`} />
                      {item.title}
                    </dt>
                    <dd>{item.explanation}</dd>
                  </div>
                ))}
              </dl>

              <h3>Виды связей</h3>
              <ul className="tight">
                {data.legend.edge_kinds.map((item) => (
                  <li key={item.id}>
                    <span className={`legend-line edge-${item.id}`}>{item.title}</span>
                    <div className="muted small">{item.explanation}</div>
                  </li>
                ))}
              </ul>

              <h3>Домены: где что живёт</h3>
              <ul className="tight">
                {data.domains.map((item) => (
                  <li key={item.id} className="domain-item">
                    <button
                      className="loc"
                      style={{ color: item.color }}
                      onClick={() => setDomain(item.id)}
                    >
                      {item.title}
                    </button>
                    <div className="muted small">{item.plain}</div>
                  </li>
                ))}
              </ul>

              <h3>Словарь</h3>
              <dl className="glossary">
                {data.legend.glossary.map((item) => (
                  <div key={item.term} className="glossary-row">
                    <dt>{item.term}</dt>
                    <dd>{item.meaning}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <>
              <h2>{active.short}</h2>
              <div className="node-kind">
                <span className={`chip kind-${active.type}`}>{active.kind_title}</span>
                <span className={`chip ${active.language}`}>
                  {active.language === 'cython' ? 'Cython' : 'Python'}
                </span>
                {owners.has(active.id) && <span className="chip owner">владеет состоянием</span>}
              </div>

              {active.plain && <p className="plain-text">{active.plain}</p>}
              <p className="muted">{active.responsibility}</p>

              <h3>Где лежит</h3>
              {(() => {
                const record = locations.get(active.source_location)
                if (!record) return <p className="muted">локация не найдена</p>
                return (
                  <p>
                    <a
                      className="loc"
                      href={sourceHref(record.file, record.line)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {record.file}:{record.line}
                    </a>
                    <br />
                    <span className="mono muted small">{record.anchor}</span>
                  </p>
                )
              })()}

              <dl className="kv">
                <dt>Тип</dt>
                <dd>{active.kind_title}</dd>
                <dt>Домен</dt>
                <dd>
                  {data.domains.find((item) => item.id === active.domain)?.title ?? active.domain}
                </dd>
                <dt>Владелец</dt>
                <dd>
                  {active.owner ?? <span className="muted">не задан — состояние общее</span>}
                </dd>
              </dl>

              <h3>Перейти</h3>
              <div className="button-row">
                <button onClick={() => onOpenSequence(active.id)}>Где это в месяце</button>
                <button onClick={() => onOpenOwners(active.id)}>Карта владения</button>
              </div>

              {outgoing.length > 0 && (
                <>
                  <h3>Отсюда вызывают ({outgoing.length})</h3>
                  {edgeList(outgoing, 'out')}
                </>
              )}
              {incoming.length > 0 && (
                <>
                  <h3>Сюда вызывают ({incoming.length})</h3>
                  {edgeList(incoming, 'in')}
                </>
              )}
              {outgoing.length === 0 && incoming.length === 0 && (
                <p className="muted">Связей в модели нет: узел либо лист, либо входная точка.</p>
              )}
            </>
          )}
        </aside>
      </div>
    </>
  )
}
