import { useEffect, useMemo, useRef, useState } from 'react'
import mermaid from 'mermaid'
import type { Architecture, ScenarioStep, SourceLocation } from './types'
import { sourceHref } from './data'

interface Props {
  data: Architecture
  selected: string | null
  onSelect: (id: string) => void
  onOpenOwners: (id: string) => void
}

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'strict',
  sequence: { useMaxWidth: false, wrap: true, diagramMarginX: 12, diagramMarginY: 10 },
})

/** Псевдоним участника: mermaid не любит длинные имена с точками и пробелами. */
function alias(name: string): string {
  return `P_${name.replace(/[^A-Za-z0-9]/g, '_')}`
}

function escapeLabel(value: string): string {
  return value.replace(/[;"\n]/g, ' ').replace(/\s+/g, ' ').slice(0, 96)
}

function buildMermaid(data: Architecture, variantId: string, focused: string | null): string {
  const scenario = data.scenarios[0]
  const variant = scenario.variants.find((item) => item.id === variantId) ?? scenario.variants[0]
  const name = (id: string) => data.nodes.find((node) => node.id === id)?.short ?? id

  const order: string[] = []
  const push = (value: string) => {
    if (!order.includes(value)) order.push(value)
  }
  for (const step of variant.steps) push(name(step.node))

  const lines: string[] = ['sequenceDiagram', '  autonumber']
  for (const entry of order) {
    lines.push(`  participant ${alias(entry)} as ${escapeLabel(entry)}`)
  }
  lines.push('  participant Engine as Economy')

  for (const step of variant.steps) {
    const from = alias(name(step.node))
    const text = escapeLabel(step.condition ? `${step.call} · если ${step.condition}` : step.call)
    lines.push(`  ${from}->>Engine: ${text}`)
    if (focused && step.node === focused) {
      lines.push('  Note right of Engine: выбранный узел')
    }
  }
  return lines.join('\n')
}

export default function SequenceView({ data, selected, onSelect, onOpenOwners }: Props) {
  const scenario = data.scenarios[0]
  const [variantId, setVariantId] = useState(scenario.variants[0].id)
  const [focused, setFocused] = useState<string | null>(selected)
  const container = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)

  const variant = scenario.variants.find((item) => item.id === variantId) ?? scenario.variants[0]

  useEffect(() => {
    if (selected) setFocused(selected)
  }, [selected])

  const definition = useMemo(() => buildMermaid(data, variantId, focused), [data, variantId, focused])

  useEffect(() => {
    let cancelled = false
    const render = async () => {
      try {
        const { svg } = await mermaid.render(`seq-${variantId}`, definition)
        if (cancelled || !container.current) return
        container.current.innerHTML = svg
        setError(null)
      } catch (exception) {
        if (cancelled) return
        setError(exception instanceof Error ? exception.message : String(exception))
      }
    }
    void render()
    return () => {
      cancelled = true
    }
  }, [definition, variantId])

  const locations = useMemo(() => {
    const map = new Map<string, SourceLocation>()
    for (const item of data.source_locations) map.set(item.id, item)
    return map
  }, [data])

  const focusedSteps: ScenarioStep[] = focused
    ? variant.steps.filter((step) => step.node === focused)
    : []

  const phases = useMemo(() => {
    const list: { phase: string; count: number }[] = []
    for (const step of variant.steps) {
      const last = list[list.length - 1]
      if (last && last.phase === step.phase) last.count += 1
      else list.push({ phase: step.phase, count: 1 })
    }
    return list
  }, [variant.steps])

  return (
    <div className="scroll-page">
      <h2>{scenario.title}: что происходит за один месяц</h2>
      <p className="muted">{scenario.description}</p>

      <div className="card">
        <h3>Сначала — простыми словами</h3>
        <p>
          Месяц в игре идёт строго по порядку, и порядок важен: сначала все смотрят на цены
          и выставляют заявки, потом торгуются, потом заводы производят, потом все считаются
          деньгами. Если переставить этапы местами, изменится результат — поэтому порядок
          записан в одном месте и показан ниже.
        </p>
        <p>
          Диаграмма читается так: сверху вниз, шаг за шагом. Слева — кто действует, стрелка — что
          он делает в этот момент, справа — сам движок экономики, на который всё в итоге
          сваливается.
        </p>
        <h3>Этапы этого варианта</h3>
        <p className="muted small">
          {phases.map((item) => `${item.phase} (${item.count})`).join(' → ')}
        </p>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '12px 0' }}>
        {scenario.variants.map((item) => (
          <button
            key={item.id}
            className={item.id === variantId ? 'active' : ''}
            onClick={() => setVariantId(item.id)}
          >
            {item.title}
          </button>
        ))}
        <span className="counter">{variant.steps.length} шагов</span>
      </div>

      <div className="card">
        <h3>{variant.title}</h3>
        <p>{variant.summary}</p>
        <h3>Чем отличаются два режима</h3>
        <ul className="tight">
          {variant.divergences.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </div>

      <div className="mermaid-host" ref={container} />
      {error && <div className="note">Mermaid не смог отрисовать диаграмму: {error}</div>}

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Все шаги месяца по порядку</h3>
        <p className="muted small">
          Колонка «Когда» показывает условие из кода: шаг выполняется только в этом режиме.
          Прочерк — шаг безусловный.
        </p>
        <table className="grid">
          <thead>
            <tr>
              <th>#</th>
              <th>Этап</th>
              <th>Что делают</th>
              <th>Когда</th>
              <th>Узел</th>
              <th>Источник</th>
            </tr>
          </thead>
          <tbody>
            {variant.steps.map((step, index) => {
              const record = locations.get(step.source_location)
              const node = data.nodes.find((item) => item.id === step.node)
              return (
                <tr
                  key={`${index}-${step.call}`}
                  className={focused === step.node ? 'selected' : ''}
                  onClick={() => {
                    setFocused(step.node)
                    onSelect(step.node)
                  }}
                >
                  <td className="mono muted">{index + 1}</td>
                  <td>{step.phase}</td>
                  <td className="wrap mono">{step.call}</td>
                  <td className="mono muted">{step.condition ?? '—'}</td>
                  <td>
                    <button
                      className="loc"
                      onClick={(event) => {
                        event.stopPropagation()
                        onOpenOwners(step.node)
                      }}
                    >
                      {node?.short ?? step.node}
                    </button>
                  </td>
                  <td>
                    {record ? (
                      <a
                        className="loc"
                        href={sourceHref(record.file, record.line)}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(event) => event.stopPropagation()}
                      >
                        {record.file.split('/').pop()}:{record.line}
                      </a>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {focused && (
        <div className="card">
          <h3>Выбранный узел в этом месяце</h3>
          <p>
            <strong>{data.nodes.find((item) => item.id === focused)?.short ?? focused}</strong>
          </p>
          {(() => {
            const node = data.nodes.find((item) => item.id === focused)
            return node?.plain ? <p className="plain-text">{node.plain}</p> : null
          })()}
          {focusedSteps.length === 0 && (
            <p className="muted">В этом варианте месяца узел не вызывается напрямую.</p>
          )}
          {focusedSteps.length > 0 && (
            <ul className="tight">
              {focusedSteps.map((step, index) => {
                const record = locations.get(step.source_location)
                return (
                  <li key={`${index}-${step.call}`}>
                    <span className="chip">{step.phase}</span>{' '}
                    <span className="mono">{step.call}</span>
                    {record && (
                      <>
                        {' — '}
                        <a
                          className="loc"
                          href={sourceHref(record.file, record.line)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {record.file}:{record.line}
                        </a>
                      </>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          <button onClick={() => onOpenOwners(focused)}>Открыть карту владения для узла</button>
        </div>
      )}
    </div>
  )
}
