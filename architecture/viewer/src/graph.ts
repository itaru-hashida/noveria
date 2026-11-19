import cytoscape from 'cytoscape'
import type { Architecture } from './types'

/**
 * Карта: узлы сгруппированы по доменам, позиции считаются детерминированно.
 *
 * Две вещи, которые пришлось выяснить на практике:
 *
 * 1. **Никаких compound-родителей.** Cytoscape молча отбрасывает ребро, если
 *    его концы лежат в разных составных узлах. Домены покрывают почти все
 *    связи (они как раз между доменами), поэтому с `parent` в графе оставалось
 *    1 ребро из 73 — снаружи это выглядело как «видна только одна стрелка».
 *    Рамки доменов рисуются обычными узлами-подложками, а связи остаются
 *    плоскими и работают.
 * 2. **Никакой force-directed раскладки.** При 74 узлах она разбрасывает точки
 *    по огромному полю, автоподгонка ужимает карту до масштаба 0.15, подписи
 *    становятся нечитаемыми и наезжают друг на друга.
 *
 * Отсюда сетка: каждый домен занимает свою прямоугольную область, узлы внутри
 * стоят в ячейках, шаг ячейки заведомо больше подписи. Перекрытий подписей нет
 * по построению, а масштаб предсказуем.
 */

const CELL_W = 168
const CELL_H = 118
const DOMAIN_PAD = 22
const DOMAIN_HEADER = 30
const DOMAIN_GAP = 30
const DOMAIN_COLUMNS = 4

const EDGE_COLORS: Record<string, string> = {
  calls: '#8b98ab',
  writes: '#3ddc84',
  owns: '#5b9dff',
  binds: '#c792ea',
  callback: '#ffa53d',
}

interface Placed {
  x: number
  y: number
}

interface Frame extends Placed {
  w: number
  h: number
}

/**
 * Перенести длинное имя по разделителям.
 *
 * Подпись — до трёх строк в узкой ячейке. Имя вида
 * `production_kernel.calculate_output` в одну строку шире ячейки и наезжает на
 * соседнюю подпись, поэтому режем его по точкам и подчёркиваниям.
 */
export function wrapLabel(value: string): string {
  const limit = 20
  if (value.length <= limit) return value
  const parts = value.split(/(?<=[._])/).filter(Boolean)
  const lines: string[] = []
  let current = ''
  for (const part of parts) {
    if (current.length + part.length > limit && current) {
      lines.push(current)
      current = part
    } else {
      current += part
    }
  }
  if (current) lines.push(current)
  return lines.join('\n')
}

/** Разложить домены и узлы по сетке. Возвращает позиции и рамки. */
export function computeLayout(data: Architecture): {
  nodePositions: Map<string, Placed>
  frames: Map<string, Frame>
} {
  const byDomain = new Map<string, string[]>()
  for (const domain of data.domains) byDomain.set(domain.id, [])
  for (const node of data.nodes) {
    const list = byDomain.get(node.domain)
    if (list) list.push(node.id)
    else byDomain.set(node.domain, [node.id])
  }

  const ordered = data.domains.filter((domain) => (byDomain.get(domain.id) ?? []).length > 0)
  const nodePositions = new Map<string, Placed>()
  const frames = new Map<string, Frame>()

  let cursorX = 0
  let cursorY = 0
  let rowHeight = 0
  let column = 0

  for (const domain of ordered) {
    const ids = byDomain.get(domain.id) ?? []
    const cols = Math.min(ids.length, Math.ceil(Math.sqrt(ids.length)))
    const rows = Math.ceil(ids.length / cols)
    const w = cols * CELL_W + DOMAIN_PAD * 2
    const h = rows * CELL_H + DOMAIN_PAD * 2 + DOMAIN_HEADER

    if (column === DOMAIN_COLUMNS) {
      cursorX = 0
      cursorY += rowHeight + DOMAIN_GAP
      rowHeight = 0
      column = 0
    }

    frames.set(domain.id, { x: cursorX + w / 2, y: cursorY + h / 2, w, h })

    ids.forEach((nodeId, index) => {
      const col = index % cols
      const rowIndex = Math.floor(index / cols)
      nodePositions.set(nodeId, {
        x: cursorX + DOMAIN_PAD + CELL_W / 2 + col * CELL_W,
        y: cursorY + DOMAIN_HEADER + DOMAIN_PAD + CELL_H / 2 + rowIndex * CELL_H,
      })
    })

    cursorX += w + DOMAIN_GAP
    rowHeight = Math.max(rowHeight, h)
    column += 1
  }

  return { nodePositions, frames }
}

export function createGraph(container: HTMLElement, data: Architecture): cytoscape.Core {
  const colors = new Map(data.domains.map((domain) => [domain.id, domain.color]))
  const { nodePositions, frames } = computeLayout(data)

  const directWriteTargets = new Set<string>()
  const callbackTargets = new Set<string>()
  for (const violation of data.violations) {
    if (violation.kind === 'direct_write' || violation.kind === 'ownerless_state') {
      for (const id of violation.related_nodes) directWriteTargets.add(id)
    }
    if (violation.kind === 'callback') {
      for (const id of violation.related_nodes) callbackTargets.add(id)
    }
  }
  const ownerNodes = new Set<string>()
  for (const owner of data.owners) {
    ownerNodes.add(owner.owner_node)
    if (owner.fallback_node) ownerNodes.add(owner.fallback_node)
  }

  const elements: cytoscape.ElementDefinition[] = []

  // Рамки доменов — обычные узлы-подложки, НЕ compound-родители: иначе
  // Cytoscape выбросит все рёбра, идущие между доменами.
  for (const [domainId, frame] of frames) {
    const domain = data.domains.find((item) => item.id === domainId)
    elements.push({
      data: {
        id: `frame:${domainId}`,
        label: domain?.title ?? domainId,
        domain: domainId,
        color: domain?.color ?? '#5f6368',
        isFrame: true,
      },
      position: { x: frame.x, y: frame.y - frame.h / 2 + 18 },
      // Размер задаётся в стиле через 'width'/'height' из данных.
      style: { width: frame.w, height: frame.h },
      grabbable: false,
      selectable: false,
      locked: true,
    })
  }

  for (const node of data.nodes) {
    const placed = nodePositions.get(node.id)
    if (!placed) continue
    const location = data.source_locations.find((item) => item.id === node.source_location)
    elements.push({
      data: {
        id: node.id,
        // Имя и тип на разных строках: сразу видно, класс это или функция.
        label: `${wrapLabel(node.short)}\n${node.kind_title}`,
        short: node.short,
        kindTitle: node.kind_title,
        domain: node.domain,
        language: node.language,
        nodeType: node.type,
        color: colors.get(node.domain) ?? '#5f6368',
        file: location?.file ?? '',
        line: location?.line ?? 0,
        selected: false,
        hasDirectWrites: directWriteTargets.has(node.id),
        hasCallbacks: callbackTargets.has(node.id),
        isOwner: ownerNodes.has(node.id),
      },
      position: placed,
    })
  }

  for (const edge of data.edges) {
    elements.push({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        kind: edge.kind,
        kindTitle: edge.kind_title,
        label: edge.kind_title,
        confidence: edge.confidence,
        dashed: edge.confidence === 'unknown',
      },
    })
  }

  const cy = cytoscape({
    container,
    elements,
    wheelSensitivity: 0.2,
    minZoom: 0.2,
    maxZoom: 4,
    style: [
      {
        selector: 'node[?isFrame]',
        style: {
          shape: 'round-rectangle',
          'background-color': 'data(color)',
          'background-opacity': 0.07,
          'border-width': 1.5,
          'border-color': 'data(color)',
          'border-opacity': 0.5,
          label: 'data(label)',
          'text-valign': 'top',
          'text-halign': 'center',
          'text-margin-y': -8,
          'font-size': 13,
          'font-weight': 700,
          color: 'data(color)',
          'text-outline-width': 2,
          'text-outline-color': '#0b0d11',
          'z-index': 0,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'node[?isFrame]',
        style: { width: 'data(width)' },
      },
      {
        selector: 'node:child',
        style: {
          'background-color': 'data(color)',
          'background-opacity': 0.95,
          'border-width': 2,
          'border-color': '#0b0d11',
          label: 'data(label)',
          color: '#f2f5f9',
          'font-size': 11,
          'font-weight': 500,
          'text-valign': 'bottom',
          'text-halign': 'center',
          'text-margin-y': 6,
          'text-wrap': 'wrap',
          'text-max-width': '150px',
          'text-outline-width': 2,
          'text-outline-color': '#0b0d11',
          'text-outline-opacity': 0.9,
          width: 34,
          height: 34,
          'z-index': 10,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          'background-opacity': 0.95,
          'border-width': 2,
          'border-color': '#0b0d11',
          label: 'data(label)',
          color: '#f2f5f9',
          'font-size': 11,
          'font-weight': 500,
          'text-valign': 'bottom',
          'text-halign': 'center',
          'text-margin-y': 6,
          'text-wrap': 'wrap',
          'text-max-width': '150px',
          'text-outline-width': 2,
          'text-outline-color': '#0b0d11',
          'text-outline-opacity': 0.9,
          width: 34,
          height: 34,
          'z-index': 10,
          'overlay-opacity': 0,
        },
      },
      { selector: 'node[?selected]', style: { 'border-width': 4, 'border-color': '#ffffff', width: 46, height: 46 } },
      { selector: 'node[?hasDirectWrites]', style: { 'border-width': 3, 'border-color': '#ff5c5c' } },
      { selector: 'node[?hasCallbacks]', style: { 'border-width': 3, 'border-color': '#ffa53d' } },
      { selector: 'node[?isOwner]', style: { 'border-width': 3, 'border-color': '#3ddc84' } },
      // Форма кодирует тип: прямоугольник — класс, круг — функция,
      // ромб — граница, шестиугольник — ядро, скруглённый — хранилище.
      { selector: 'node[nodeType = "class"]', style: { shape: 'rectangle', width: 48, height: 32 } },
      { selector: 'node[nodeType = "store"]', style: { shape: 'round-rectangle', width: 58, height: 36 } },
      { selector: 'node[nodeType = "kernel"]', style: { shape: 'hexagon', width: 44, height: 44 } },
      { selector: 'node[nodeType = "boundary"]', style: { shape: 'diamond', width: 40, height: 40 } },
      { selector: 'node[language = "cython"]', style: { 'border-style': 'double', 'border-width': 3 } },
      {
        selector: 'edge',
        style: {
          width: 2.2,
          'line-color': '#8b98ab',
          'target-arrow-color': '#8b98ab',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'arrow-scale': 1.1,
          'font-size': 9,
          color: '#dfe6f0',
          'text-opacity': 0,
          'text-rotation': 'autorotate',
          'text-background-color': '#0b0d11',
          'text-background-opacity': 0.9,
          'text-background-padding': '2px',
          'z-index': 5,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'edge[kind = "calls"]',
        style: { 'line-color': EDGE_COLORS.calls, 'target-arrow-color': EDGE_COLORS.calls },
      },
      {
        selector: 'edge[kind = "writes"]',
        style: { 'line-color': EDGE_COLORS.writes, 'target-arrow-color': EDGE_COLORS.writes, width: 2.6 },
      },
      {
        selector: 'edge[kind = "owns"]',
        style: {
          'line-color': EDGE_COLORS.owns,
          'target-arrow-color': EDGE_COLORS.owns,
          width: 2.6,
          'arrow-scale': 1.3,
        },
      },
      {
        selector: 'edge[kind = "binds"]',
        style: { 'line-color': EDGE_COLORS.binds, 'target-arrow-color': EDGE_COLORS.binds, 'line-style': 'dashed' },
      },
      {
        selector: 'edge[kind = "callback"]',
        style: { 'line-color': EDGE_COLORS.callback, 'target-arrow-color': EDGE_COLORS.callback, width: 3 },
      },
      { selector: 'edge[?dashed]', style: { 'line-style': 'dotted' } },
      {
        selector: 'edge:selected',
        style: { 'line-color': '#ffffff', 'target-arrow-color': '#ffffff', width: 3.4, 'text-opacity': 1 },
      },
      { selector: 'edge.highlighted', style: { width: 3.2, 'text-opacity': 1, 'z-index': 20 } },
      { selector: 'node.neighbor', style: { 'border-width': 3, 'border-color': '#ffffff' } },
      { selector: 'node.dimmed', style: { opacity: 0.25 } },
      { selector: 'edge.dimmed', style: { opacity: 0.12 } },
      { selector: '.filtered', style: { display: 'none' } },
    ],
    // Позиции уже посчитаны: раскладку не запускаем, иначе она всё сдвинет.
    layout: { name: 'preset', fit: true, padding: 40 },
  })

  // Размер рамки задаётся атрибутами style из данных элемента, но cytoscape
  // надёжнее принимает его через bypass — иначе 'width: data(...)' не работает.
  for (const [domainId, frame] of frames) {
    const element = cy.getElementById(`frame:${domainId}`)
    if (element.empty()) continue
    element.style({ width: frame.w, height: frame.h })
  }

  // Стартовый масштаб: после fit() карта целиком ужимается до ~0.3, и подписи
  // становятся мелкими. Это решение пользователя, но по умолчанию лучше
  // показать читаемый центр, чем всю простыню целиком: колесо и перетаскивание
  // никуда не деваются.
  const fitted = cy.zoom()
  const readable = Math.min(Math.max(fitted, 0.55), 1.1)
  if (readable > fitted) {
    cy.zoom({ level: readable, renderedPosition: { x: 0, y: 0 } })
    cy.center(cy.nodes('[?isFrame]'))
  }

  return cy
}

/**
 * Применить набор видимых узлов: скрыть остальные и связи с ними.
 *
 * Позиции не меняются: у каждого узла своё постоянное место в сетке домена,
 * поэтому карта не «прыгает» при фильтрации и подписи не переезжают.
 */
export function applyVisibility(cy: cytoscape.Core, visible: Set<string>): void {
  cy.batch(() => {
    cy.nodes().not('[?isFrame]').forEach((node) => {
      if (visible.has(node.id())) node.removeClass('filtered')
      else node.addClass('filtered')
    })
    cy.edges().forEach((edge) => {
      const source = edge.data('source')
      const target = edge.data('target')
      if (visible.has(source) && visible.has(target)) edge.removeClass('filtered')
      else edge.addClass('filtered')
    })
    // Пустые рамки домена скрываем: они занимали бы место без пользы.
    cy.nodes('[?isFrame]').forEach((frame) => {
      const domainId = frame.data('domain')
      const any = cy
        .nodes()
        .not('[?isFrame]')
        .filter((node) => node.data('domain') === domainId && !node.hasClass('filtered'))
      if (any.length === 0) frame.addClass('filtered')
      else frame.removeClass('filtered')
    })
  })

  const shown = cy.nodes().not('[?isFrame]').filter((node) => !node.hasClass('filtered'))
  if (shown.length === 0) return
  const visibleFrames = cy.nodes('[?isFrame]').filter((frame) => !frame.hasClass('filtered'))
  cy.fit(shown.union(visibleFrames), 50)
}

/**
 * Подсветить выбранный узел и его окружение, приглушив остальное.
 *
 * Без этого на 74 узлах невозможно понять, куда смотреть: карта выглядит
 * одинаково при любом клике.
 */
export function focusNode(cy: cytoscape.Core, id: string | null): void {
  cy.batch(() => {
    cy.elements().removeClass('highlighted neighbor dimmed')
    cy.elements(':selected').unselect()
    if (!id) return
    const node = cy.getElementById(id)
    if (node.empty()) return
    node.addClass('neighbor')
    const connected = node.connectedEdges()
    connected.addClass('highlighted')
    const related = connected.connectedNodes()
    related.addClass('neighbor')
    const keep = node.union(connected).union(related)
    cy.elements()
      .not(keep)
      .not(cy.nodes('[?isFrame]'))
      .addClass('dimmed')
  })
}
