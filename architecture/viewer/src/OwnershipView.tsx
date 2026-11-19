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

const severityTitle: Record<string, string> = {
  high: 'важно',
  medium: 'заметно',
  low: 'мелочь',
  info: 'справка',
}

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
        owner.plain_what,
        owner.plain_how,
        ...owner.readers.map(nodeName),
        ...owner.writers.map(nodeName),
      ]
        .join(' ')
        .toLowerCase()
      return needle.split(/\s+/).every((part) => haystack.includes(part))
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.owners, data.nodes, query])

  const violations = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return data.violations
      .filter((item) => {
        if (onlyProblems && item.severity === 'info') return false
        if (!needle) return true
        const haystack = [
          item.title,
          item.description,
          item.plain,
          item.why,
          item.kind,
          item.state,
          item.mitigation ?? '',
        ]
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

  const focusViolations = useMemo(
    () => (selected ? data.violations.filter((item) => item.related_nodes.includes(selected)) : []),
    [data.violations, selected],
  )

  return (
    <div className="scroll-page">
      <h2>Кто чем владеет — и где это нарушают</h2>
      <p className="muted">
        Главная мысль: у важных данных должен быть один хозяин. Тогда никто не может незаметно
        переписать сумму и никто не спорит, чья правда вернее. Ниже — по каждому важному
        состоянию: где оно лежит, через что его правильно менять и кто всё-таки лезет мимо.
      </p>

      <div className="card">
        <h3>Как это устроено в двух словах</h3>
        <p>
          Деньги, склад и заявки охраняются: их меняет только специальный объект-хранилище, а всё
          остальное обязано его просить. Внутри месяца попытка записать мимо хозяина — это ошибка,
          а не тихое исправление.
        </p>
        <p>
          Хуже там, где хозяина нет: проекты и стройки, план производства, результаты выпуска.
          Там несколько мест пишут в одни и те же поля, и договориться им помогает только
          аккуратность автора.
        </p>
      </div>

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
          <p>
            <span className={`chip kind-${focus.type}`}>{focus.kind_title}</span>{' '}
            <span className={`chip ${focus.language}`}>{focus.language}</span>
          </p>
          {focus.plain && <p className="plain-text">{focus.plain}</p>}
          <dl className="kv">
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
              <h3>Пишет в</h3>
              <ul className="tight">
                {related.writes.map((edge) => (
                  <li key={edge.id}>
                    &rarr; {nodeName(edge.target)}
                    <div className="muted small">{edge.plain}</div>
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
              <h3>Обратные вызовы</h3>
              <ul className="tight">
                {related.callbacks.map((edge) => (
                  <li key={edge.id}>
                    {nodeName(edge.source)} &rarr; {nodeName(edge.target)}
                    <div className="muted small">{edge.plain}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {focusViolations.length > 0 && (
            <>
              <h3>Нарушения рядом</h3>
              <ul className="tight">
                {focusViolations.map((item) => (
                  <li key={item.id}>
                    <span className="chip violation">{item.kind_title}</span> {item.title}
                    <div className="muted small">{item.plain}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
          <div className="button-row">
            <button onClick={() => onOpenSequence(focus.id)}>Где это в месяце</button>
          </div>
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
            <p className="plain-text">{owner.plain_what}</p>
            <p>{owner.plain_how}</p>
            <p className="muted">{owner.plain_care}</p>

            <dl className="kv">
              <dt>Хранится</dt>
              <dd>{owner.storage}</dd>
              <dt>Хозяин</dt>
              <dd>
                <button className="loc" onClick={() => onSelect(owner.owner_node)}>
                  {ownerNode?.short ?? owner.owner_node}
                </button>
                {fallbackNode && (
                  <>
                    {' · запасной: '}
                    <button className="loc" onClick={() => onSelect(fallbackNode.id)}>
                      {fallbackNode.short}
                    </button>
                  </>
                )}
              </dd>
              <dt>Правильно</dt>
              <dd className="mono small">{owner.write_path}</dd>
            </dl>

            <h3>Кто читает</h3>
            <ul className="tight">
              {owner.readers.map((id) => (
                <li key={id}>
                  <button className="loc" onClick={() => onSelect(id)}>
                    {nodeName(id)}
                  </button>
                </li>
              ))}
            </ul>

            <h3>Кто меняет</h3>
            <ul className="tight">
              {owner.writers.map((id) => (
                <li key={id}>
                  <button className="loc" onClick={() => onSelect(id)}>
                    {nodeName(id)}
                  </button>
                </li>
              ))}
            </ul>

            <h3>Записи мимо хозяина</h3>
            <ul className="tight">
              {[...owner.direct_writes, ...owner.callbacks].map((id) => {
                const item = violationById.get(id)
                if (!item) {
                  return (
                    <li key={id}>
                      <span className="chip violation">не найдено</span> {id}
                    </li>
                  )
                }
                const record = locations.get(item.source_location)
                return (
                  <li key={id}>
                    <span className={`chip ${item.kind === 'callback' ? 'callback' : 'violation'}`}>
                      {item.kind_title}
                    </span>{' '}
                    {item.title}
                    <div className="muted small">{item.plain}</div>
                    {record && (
                      <a
                        className="loc small"
                        href={sourceHref(record.file, record.line)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {record.file.split('/').pop()}:{record.line}
                      </a>
                    )}
                  </li>
                )
              })}
              {owner.direct_writes.length === 0 && owner.callbacks.length === 0 && (
                <li className="muted">Прямых записей не найдено — всё через хозяина.</li>
              )}
            </ul>

            <h3>Заметки</h3>
            <p className="muted small">{owner.notes}</p>
            {owner.evidence.length > 0 && (
              <>
                <h3>Чем подтверждено</h3>
                <ul className="tight">
                  {owner.evidence.map((item) => (
                    <li key={item} className="mono small">
                      {item}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )
      })}

      <h2 style={{ marginTop: 26 }}>Нарушения: что не так и почему это важно</h2>
      <div className="card">
        <h3>Как читать таблицу</h3>
        <ul className="tight">
          {data.legend.violation_kinds.map((item) => (
            <li key={item.id}>
              <strong>{item.title}</strong>
              <div className="muted small">{item.explanation}</div>
            </li>
          ))}
        </ul>
        <p className="muted small">
          Уровень «справка» — это не поломка, а осознанное решение, о котором стоит знать.
        </p>
      </div>

      <table className="grid">
        <thead>
          <tr>
            <th>Уровень</th>
            <th>Тип</th>
            <th>Что не так</th>
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
                    {severityTitle[item.severity] ?? item.severity}
                  </span>
                </td>
                <td className="small">{item.kind_title}</td>
                <td className="wrap">
                  <strong>{item.title}</strong>
                  <div className="small" style={{ marginTop: 4 }}>
                    {item.plain}
                  </div>
                  {item.why && (
                    <div className="muted small" style={{ marginTop: 4 }}>
                      Почему важно: {item.why}
                    </div>
                  )}
                  {item.mitigation && (
                    <div className="muted small" style={{ marginTop: 4 }}>
                      Что делать: {item.mitigation}
                    </div>
                  )}
                </td>
                <td>
                  {record ? (
                    <a
                      className="loc"
                      href={sourceHref(record.file, record.line)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {record.file.split('/').pop()}:{record.line}
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
