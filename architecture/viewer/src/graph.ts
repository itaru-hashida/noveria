import cytoscape from 'cytoscape'
import type { Architecture } from './types'

/**
 * Граф строится один раз и целиком: все узлы и все связи.
 *
 * Фильтр не пересобирает элементы, а скрывает их классом `filtered`
 * (`display: none`). Цикл remove/add в cytoscape ломается: при повторном
 * добавлении он восстанавливает удалённые элементы и падает на ребре, чей
 * источник ещё не восстановлен. Скрытие через стиль такой проблемы не имеет и
 * заодно сохраняет позиции узлов между фильтрами.
 */
export function createGraph(container: HTMLElement, data: Architecture): cytoscape.Core {
  const colors = new Map(data.domains.map((domain) => [domain.id, domain.color]))

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
  for (const node of data.nodes) {
    elements.push({
      data: {
        id: node.id,
        label: node.short,
        domain: node.domain,
        language: node.language,
        nodeType: node.type,
        color: colors.get(node.domain) ?? '#5f6368',
        selected: false,
        hasDirectWrites: directWriteTargets.has(node.id),
        hasCallbacks: callbackTargets.has(node.id),
        isOwner: ownerNodes.has(node.id),
      },
    })
  }
  for (const edge of data.edges) {
    elements.push({
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        kind: edge.kind,
        label: edge.label,
        confidence: edge.confidence,
        dashed: edge.confidence === 'unknown' || edge.kind === 'callback',
      },
    })
  }

  return cytoscape({
    container,
    elements,
    wheelSensitivity: 0.2,
    style: [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          'border-width': 1,
          'border-color': '#0f1115',
          label: 'data(label)',
          color: '#e6e9ee',
          'font-size': 10,
          'text-valign': 'bottom',
          'text-margin-y': 4,
          'text-wrap': 'wrap',
          'text-max-width': '150px',
          width: 26,
          height: 26,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'node[?selected]',
        style: { 'border-width': 3, 'border-color': '#ffffff', width: 34, height: 34 },
      },
      { selector: 'node[?hasDirectWrites]', style: { 'border-width': 3, 'border-color': '#d64545' } },
      { selector: 'node[?hasCallbacks]', style: { shape: 'diamond', width: 30, height: 30 } },
      { selector: 'node[?isOwner]', style: { 'border-width': 3, 'border-color': '#2e9e63' } },
      {
        selector: 'node[nodeType = "store"]',
        style: { shape: 'round-rectangle', width: 40, height: 30 },
      },
      { selector: 'node[nodeType = "kernel"]', style: { shape: 'hexagon', width: 32, height: 32 } },
      { selector: 'node[nodeType = "boundary"]', style: { shape: 'diamond', width: 28, height: 28 } },
      { selector: 'node[language = "cython"]', style: { 'border-color': '#b06000' } },
      {
        selector: 'edge',
        style: {
          width: 1.2,
          'line-color': '#3a4351',
          'target-arrow-color': '#3a4351',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'arrow-scale': 0.8,
          'font-size': 8,
          color: '#9aa4b2',
          'text-opacity': 0,
          'overlay-opacity': 0,
        },
      },
      { selector: 'edge[?dashed]', style: { 'line-style': 'dashed' } },
      {
        selector: 'edge[kind = "writes"]',
        style: { 'line-color': '#2e9e63', 'target-arrow-color': '#2e9e63' },
      },
      {
        selector: 'edge[kind = "callback"]',
        style: { 'line-color': '#e8710a', 'target-arrow-color': '#e8710a', width: 1.8 },
      },
      {
        selector: 'edge[kind = "owns"]',
        style: { 'line-color': '#5b7cfa', 'target-arrow-color': '#5b7cfa', 'arrow-scale': 1.1 },
      },
      {
        selector: 'edge:selected',
        style: { 'line-color': '#ffffff', 'target-arrow-color': '#ffffff', width: 2.2 },
      },
      { selector: 'node:selected', style: { 'border-width': 4, 'border-color': '#ffffff' } },
      { selector: '.filtered', style: { display: 'none' } },
    ],
    layout: {
      name: 'cose',
      animate: false,
      randomize: true,
      nodeRepulsion: 9000,
      idealEdgeLength: 90,
      padding: 30,
    },
  })
}

/**
 * Применить набор видимых узлов: скрыть остальные и связи с ними.
 *
 * Раскладка пересчитывается по видимой части: иначе скрытые узлы оставляли бы
 * дыры, а карта домена выглядела бы как случайные точки.
 */
export function applyVisibility(cy: cytoscape.Core, visible: Set<string>): void {
  cy.batch(() => {
    cy.nodes().forEach((node) => {
      if (visible.has(node.id())) node.removeClass('filtered')
      else node.addClass('filtered')
    })
    cy.edges().forEach((edge) => {
      const source = edge.data('source')
      const target = edge.data('target')
      if (visible.has(source) && visible.has(target)) edge.removeClass('filtered')
      else edge.addClass('filtered')
    })
  })

  const shown = cy.nodes().filter((node) => !node.hasClass('filtered'))
  if (shown.length === 0) return
  const shownEdges = cy.edges().filter((edge) => !edge.hasClass('filtered'))
  const layout = shown.layout({
    name: 'cose',
    animate: false,
    randomize: true,
    nodeRepulsion: 9000,
    idealEdgeLength: 90,
    padding: 30,
  })
  layout.run()
  void shownEdges
  cy.fit(shown, 40)
}
