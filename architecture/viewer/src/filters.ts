/**
 * Чистые функции фильтрации карты.
 *
 * Вынесены из React-компонента, чтобы их можно было проверить без браузера:
 * `node --test` (Node 24 исполняет TypeScript напрямую). Логика совпадает с
 * тем, что вызывает GraphView, поэтому проверка фильтров — это проверка
 * реального кода, а не его копии.
 */
import type { Architecture, ArchNode } from './types'

export interface FilterState {
  query: string
  domain: string
  languages: Set<string>
  ownersOnly: boolean
  writesOnly: boolean
  callbacksOnly: boolean
}

export const emptyFilters: FilterState = {
  query: '',
  domain: '',
  languages: new Set(),
  ownersOnly: false,
  writesOnly: false,
  callbacksOnly: false,
}

/** Совпадает ли узел с поисковой строкой: имя, файл, символ, ответственность.
 *
 * Строка запроса режется на слова, и каждое слово обязано найтись — это AND.
 * Дополнительно в индекс входит пара `файл:строка`, поэтому запрос вида
 * `cash_store.pyx:423` находит узел по точной локации.
 */
export function matchesQuery(data: Architecture, node: ArchNode, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  const location = data.source_locations.find((item) => item.id === node.source_location)
  const haystack = [
    node.name,
    node.short,
    node.responsibility,
    node.type,
    node.domain,
    node.owner ?? '',
    location?.file ?? '',
    location?.symbol ?? '',
    String(location?.line ?? ''),
    location ? `${location.file}:${location.line}` : '',
    location ? `${location.file.split('/').pop()}:${location.line}` : '',
    ...node.tags,
  ]
    .join(' ')
    .toLowerCase()
  return needle.split(/\s+/).every((part) => haystack.includes(part))
}

/** Узлы, которые владеют состоянием (store или явный владелец). */
export function ownerNodeIds(data: Architecture): Set<string> {
  const ids = new Set<string>()
  for (const owner of data.owners) {
    ids.add(owner.owner_node)
    if (owner.fallback_node) ids.add(owner.fallback_node)
  }
  return ids
}

/** Узлы, у которых есть прямые записи по данным нарушений. */
export function directWriteNodeIds(data: Architecture): Set<string> {
  const ids = new Set<string>()
  for (const violation of data.violations) {
    if (violation.kind === 'direct_write' || violation.kind === 'ownerless_state') {
      for (const id of violation.related_nodes) ids.add(id)
    }
  }
  return ids
}

/** Узлы, участвующие в callback-переходах из горячего цикла. */
export function callbackNodeIds(data: Architecture): Set<string> {
  const ids = new Set<string>()
  for (const violation of data.violations) {
    if (violation.kind !== 'callback') continue
    for (const id of violation.related_nodes) ids.add(id)
  }
  return ids
}

/** Итоговый набор видимых узлов: поиск + фильтры + режимы подсветки. */
export function visibleNodeIds(data: Architecture, filters: FilterState): Set<string> {
  const owners = filters.ownersOnly ? ownerNodeIds(data) : null
  const writes = filters.writesOnly ? directWriteNodeIds(data) : null
  const callbacks = filters.callbacksOnly ? callbackNodeIds(data) : null

  const ids = new Set<string>()
  for (const node of data.nodes) {
    if (!matchesQuery(data, node, filters.query)) continue
    if (filters.domain && node.domain !== filters.domain) continue
    if (filters.languages.size && !filters.languages.has(node.language)) continue
    if (owners && !owners.has(node.id)) continue
    if (writes && !writes.has(node.id)) continue
    if (callbacks && !callbacks.has(node.id)) continue
    ids.add(node.id)
  }
  return ids
}

/** Рёбра, оба конца которых видимы. */
export function visibleEdgeIds(data: Architecture, visibleNodes: Set<string>): Set<string> {
  const ids = new Set<string>()
  for (const edge of data.edges) {
    if (visibleNodes.has(edge.source) && visibleNodes.has(edge.target)) ids.add(edge.id)
  }
  return ids
}
