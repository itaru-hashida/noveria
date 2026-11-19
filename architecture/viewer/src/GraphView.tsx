import { useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Architecture, ArchEdge, SourceLocation } from './types'
import { applyVisibility, createGraph } from './graph'
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

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.nodes().forEach((node) => {
      node.data('selected', node.id() === selected)
    })
    if (selected) {
      const node = cy.getElementById(selected)
      if (node.nonempty()) {
        cy.stop()
        cy.animate({ center: { eles: node }, zoom: 1.05 }, { duration: 200 })
        node.select()
      }
    } else {
      cy.$(':selected').unselect()
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
            <span className={`chip ${edge.kind}`}>{edge.kind}</span>{' '}
            <span className="muted">{edge.label}</span>
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
        >
          Python
        </button>
        <button
          className={languages.has('cython') ? 'active' : ''}
          onClick={() => toggleLanguage('cython')}
        >
          Cython
        </button>
        <button className={ownersOnly ? 'active' : ''} onClick={() => setOwnersOnly(!ownersOnly)}>
          Владельцы
        </button>
        <button className={writesOnly ? 'active' : ''} onClick={() => setWritesOnly(!writesOnly)}>
          Прямые записи
        </button>
        <button
          className={callbacksOnly ? 'active' : ''}
          onClick={() => setCallbacksOnly(!callbacksOnly)}
        >
          Callbacks
        </button>
        <span className="counter">
          {visible.size} / {data.nodes.length} узлов
        </span>
      </div>

      <div className="body">
        <div className="graph-wrap">
          <div id="cy" ref={canvas} />
        </div>

        <aside className="side">
          {!active ? (
            <>
              <h2>Общая карта экономики</h2>
              <p className="muted">
                Узлы — классы, функции, store, ядра и границы. Рёбра — реальные вызовы, записи,
                владение и callback-переходы. Нажмите узел, чтобы увидеть файл и строку.
              </p>
              <h3>Домены</h3>
              <ul className="tight">
                {data.domains.map((item) => (
                  <li key={item.id}>
                    <button
                      className="loc"
                      style={{ color: item.color }}
                      onClick={() => setDomain(item.id)}
                    >
                      {item.title}
                    </button>{' '}
                    <span className="muted">{item.description}</span>
                  </li>
                ))}
              </ul>
              <h3>Обозначения</h3>
              <dl className="kv">
                <dt>Шестиугольник</dt>
                <dd>Cython-ядро (kernel)</dd>
                <dt>Ромб</dt>
                <dd>Граница Python↔Cython или callback-узел</dd>
                <dt>Скруглённый</dt>
                <dd>Store — владелец состояния</dd>
                <dt>Зелёная рамка</dt>
                <dd>Владелец состояния</dd>
                <dt>Красная рамка</dt>
                <dd>Есть прямые записи</dd>
                <dt>Оранжевое ребро</dt>
                <dd>Callback из горячего цикла</dd>
              </dl>
              <h3>Покрытие анализа</h3>
              <p className="muted">{data.meta.scope_note}</p>
              <p className="muted mono">
                Python-файлов: {data.meta.python_files.length}, Cython:{' '}
                {data.meta.cython_files.length}, расширений собрано:{' '}
                {data.meta.native_extensions_built ? data.meta.native_extension_count : 0}
              </p>
            </>
          ) : (
            <>
              <h2>{active.short}</h2>
              <p className="muted">{active.responsibility}</p>
              <dl className="kv">
                <dt>Имя</dt>
                <dd className="mono">{active.name}</dd>
                <dt>Тип</dt>
                <dd>
                  <span className="chip">{active.type}</span>
                </dd>
                <dt>Домен</dt>
                <dd>
                  {data.domains.find((item) => item.id === active.domain)?.title ?? active.domain}
                </dd>
                <dt>Язык</dt>
                <dd>
                  <span className={`chip ${active.language}`}>{active.language}</span>
                </dd>
                <dt>Владелец</dt>
                <dd>
                  {active.owner ?? <span className="muted">не задан</span>}
                  {owners.has(active.id) && <span className="chip owner"> store</span>}
                </dd>
              </dl>

              <h3>Источник</h3>
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
                    <span className="mono muted">{record.anchor}</span>
                  </p>
                )
              })()}

              <h3>Переходы</h3>
              <div>
                <button onClick={() => onOpenSequence(active.id)}>Место в месячном ходе</button>{' '}
                <button onClick={() => onOpenOwners(active.id)}>Карта владения</button>
              </div>

              {outgoing.length > 0 && (
                <>
                  <h3>Исходящие ({outgoing.length})</h3>
                  {edgeList(outgoing, 'out')}
                </>
              )}
              {incoming.length > 0 && (
                <>
                  <h3>Входящие ({incoming.length})</h3>
                  {edgeList(incoming, 'in')}
                </>
              )}
            </>
          )}
        </aside>
      </div>
    </>
  )
}
