import { useMemo, useState } from 'react'
import type { Architecture, SourceLocation } from './types'
import { sourceHref } from './data'

interface Props {
  data: Architecture
  selected: string | null
  onSelect: (id: string) => void
  onOpenSequence: (id: string) => void
}

const severityOrder: Record<string, number> = { high: 0, medium: 1, low: 2, info: 3 }

export default function OwnershipView({ data, selected, onSelect, onOpenSequence }: Props) {
  const [query, setQuery] = useState('')
  const [onlyProblems, setOnlyProblems] = useState(false)

  const locations = useMemo(() => {
    const map = new Map<string, SourceLocation>()
    for (const item of data.source_locations) map.set(item.id, item)
    return map
  }, [data])

  const nodeName = (id: string) => data.nodes.find((node) => node.id === id)?.short ?? id

  const violationById = useMemo(
    () => new Map(data.violations.map((item) => [item.id, item])),
    [data],
  )

  const owners = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return data.owners.filter((owner) => {
      if (!needle) return true
      const haystack = [
        owner.title,
        owner.state,
        owner.storage,
        owner.write_path,
        owner.notes,
        ...owner.readers.map(nodeName),
        ...owner.writers.map(nodeName),
      ]
        .join(' ')
        .toLowerCase()
      return needle.split(/\s+/).every((part) => haystack.includes(part))
    })
    // nodeName зависит только от data, которая уже в зависимостях.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.owners, data.nodes, query])

  const violations = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return data.violations
      .filter((item) => {
        if (onlyProblems && item.severity === 'info') return false
        if (!needle) return true
        const haystack = [item.title, item.description, item.kind, item.state, item.mitigation ?? '']
          .concat(item.related_nodes.map(nodeName))
          .join(' ')
          .toLowerCase()
        return needle.split(/\s+/).every((part) => haystack.includes(part))
      })
      .sort((left, right) => (severityOrder[left.severity] ?? 9) - (severityOrder[right.severity] ?? 9))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.violations, data.nodes, query, onlyProblems])

  const focus = selected ? data.nodes.find((node) => node.id === selected) ?? null : null

  const related = useMemo(() => {
    if (!selected) return null
    const writes = data.edges.filter((edge) => edge.source === selected && edge.kind === 'writes')
    const owned = data.edges.filter((edge) => edge.source === selected && edge.kind === 'owns')
    const callbacks = data.edges.filter(
      (edge) => edge.kind === 'callback' && (edge.source === selected || edge.target === selected),
    )
    const ownersOfNode = data.owners.filter(
      (owner) => owner.owner_node === selected || owner.fallback_node === selected,
    )
    return { writes, owned, callbacks, ownersOfNode }
  }, [data, selected])

  return (
    <div className="scroll-page">
      <h2>Карта владения и записей</h2>
      <p className="muted">
        Для каждого ключевого состояния: где лежит, кто читает, кто пишет, через какой метод должна
        идти запись и какие места пишут напрямую. Нарушения не скрываются: они перечислены отдельной
        таблицей с типом и уровнем.
      </p>

      <div className="toolbar" style={{ background: 'transparent', border: 'none', padding: '10px 0' }}>
        <input
          className="grow"
          type="search"
          placeholder="Поиск по состоянию, владельцу, нарушению…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button className={onlyProblems ? 'active' : ''} onClick={() => setOnlyProblems(!onlyProblems)}>
          Только проблемы
        </button>
        <span className="counter">
          {owners.length} состояний · {violations.length} нарушений
        </span>
      </div>

      {focus && related && (
        <div className="card">
          <h3>Узел «{focus.short}» в карте владения</h3>
          <dl className="kv">
            <dt>Тип</dt>
            <dd>
              <span className="chip">{focus.type}</span>{' '}
              <span className={`chip ${focus.language}`}>{focus.language}</span>
            </dd>
            <dt>Владелец</dt>
            <dd>{focus.owner ?? <span className="muted">не задан</span>}</dd>
            <dt>Домен</dt>
            <dd>{data.domains.find((domain) => domain.id === focus.domain)?.title ?? focus.domain}</dd>
          </dl>
          {related.ownersOfNode.length > 0 && (
            <>
              <h3>Владеет состоянием</h3>
              <ul className="tight">
                {related.ownersOfNode.map((owner) => (
                  <li key={owner.id}>
                    «{owner.title}» — {owner.state}
                  </li>
                ))}
              </ul>
            </>
          )}
          {related.writes.length > 0 && (
            <>
              <h3>Пишет</h3>
              <ul className="tight">
                {related.writes.map((edge) => (
                  <li key={edge.id}>
                    &rarr; {nodeName(edge.target)} <span className="muted">{edge.label}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {related.owned.length > 0 && (
            <>
              <h3>Владеет</h3>
              <ul className="tight">
                {related.owned.map((edge) => (
                  <li key={edge.id}>&rarr; {nodeName(edge.target)}</li>
                ))}
              </ul>
            </>
          )}
          {related.callbacks.length > 0 && (
            <>
              <h3>Callback-переходы</h3>
              <ul className="tight">
                {related.callbacks.map((edge) => (
                  <li key={edge.id}>
                    {nodeName(edge.source)} &rarr; {nodeName(edge.target)}{' '}
                    <span className="muted">{edge.label}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          <button onClick={() => onOpenSequence(focus.id)}>Показать место в месячном ходе</button>
        </div>
      )}

      {owners.map((owner) => {
        const ownerNode = data.nodes.find((node) => node.id === owner.owner_node)
        const fallbackNode = owner.fallback_node
          ? data.nodes.find((node) => node.id === owner.fallback_node)
          : null
        return (
          <div className="card" key={owner.id}>
            <h3>{owner.title}</h3>
            <dl className="kv">
              <dt>Состояние</dt>
              <dd>{owner.state}</dd>
              <dt>Хранится</dt>
              <dd>{owner.storage}</dd>
              <dt>Владелец</dt>
              <dd>
                <button className="loc" onClick={() => onSelect(owner.owner_node)}>
                  {ownerNode?.short ?? owner.owner_node}
                </button>
                {fallbackNode && (
                  <>
                    {' · fallback: '}
                    <button className="loc" onClick={() => onSelect(fallbackNode.id)}>
                      {fallbackNode.short}
                    </button>
                  </>
                )}
              </dd>
              <dt>Запись</dt>
              <dd className="mono">{owner.write_path}</dd>
            </dl>

            <h3>Читают</h3>
            <ul className="tight">
              {owner.readers.map((id) => (
                <li key={id}>
                  <button className="loc" onClick={() => onSelect(id)}>
                    {nodeName(id)}
                  </button>
                </li>
              ))}
            </ul>

            <h3>Изменяют</h3>
            <ul className="tight">
              {owner.writers.map((id) => (
                <li key={id}>
                  <button className="loc" onClick={() => onSelect(id)}>
                    {nodeName(id)}
                  </button>
                </li>
              ))}
            </ul>

            <h3>Прямые записи и callback</h3>
            <ul className="tight">
              {[...owner.direct_writes, ...owner.callbacks].map((id) => {
                const item = violationById.get(id)
                if (!item) {
                  return (
                    <li key={id}>
                      <span className="chip violation">unknown</span> {id}
                    </li>
                  )
                }
                const record = locations.get(item.source_location)
                return (
                  <li key={id}>
                    <span className={`chip ${item.kind === 'callback' ? 'callback' : 'violation'}`}>
                      {item.kind}
                    </span>{' '}
                    {item.title}
                    {record && (
                      <>
                        {' — '}
                        <a
                          className="loc"
                          href={sourceHref(record.file, record.line)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {record.file.split('/').pop()}:{record.line}
                        </a>
                      </>
                    )}
                  </li>
                )
              })}
              {owner.direct_writes.length === 0 && owner.callbacks.length === 0 && (
                <li className="muted">Прямых записей не найдено.</li>
              )}
            </ul>

            <h3>Заметки</h3>
            <p>{owner.notes}</p>
            {owner.evidence.length > 0 && (
              <>
                <h3>Подтверждающие тесты</h3>
                <ul className="tight">
                  {owner.evidence.map((item) => (
                    <li key={item} className="mono">
                      {item}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )
      })}

      <h2 style={{ marginTop: 26 }}>Нарушения и зоны риска</h2>
      <p className="muted">
        Тип «direct_write» — запись мимо владельца; «ownerless_state» — состояние без владельца;
        «callback» — переход из горячего цикла в Python; «boundary_leak» — протечка границы между
        слоями. Уровень «info» помечает осознанные границы, а не дефекты.
      </p>
      <table className="grid">
        <thead>
          <tr>
            <th>Уровень</th>
            <th>Тип</th>
            <th>Нарушение</th>
            <th>Состояние</th>
            <th>Где</th>
            <th>Связанные узлы</th>
          </tr>
        </thead>
        <tbody>
          {violations.map((item) => {
            const record = locations.get(item.source_location)
            return (
              <tr
                key={item.id}
                className={item.related_nodes.includes(selected ?? '') ? 'selected' : ''}
              >
                <td>
                  <span className={`chip ${item.severity === 'info' ? '' : 'violation'}`}>
                    {item.severity}
                  </span>
                </td>
                <td className="mono">{item.kind}</td>
                <td className="wrap">
                  <strong>{item.title}</strong>
                  <div className="muted">{item.description}</div>
                  {item.mitigation && <div className="muted">Что делать: {item.mitigation}</div>}
                </td>
                <td>{item.state}</td>
                <td>
                  {record ? (
                    <a
                      className="loc"
                      href={sourceHref(record.file, record.line)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {record.file}:{record.line}
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
                <td>
                  {item.related_nodes.map((id) => (
                    <div key={id}>
                      <button className="loc" onClick={() => onSelect(id)}>
                        {nodeName(id)}
                      </button>
                    </div>
                  ))}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
