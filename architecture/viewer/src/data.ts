import type { Architecture } from './types'

/**
 * База просмотра: viewer открывает файл по ссылке `file:line`.
 *
 * В репозитории файл адресуется относительно корня, поэтому ссылка «открыть
 * файл и строку» — это редакторская ссылка на корень репозитория. База
 * задаётся переменной окружения, чтобы один и тот же viewer работал и с
 * локальным file://, и с редактором по умолчанию.
 */
export const REPO_BASE: string =
  (import.meta.env.VITE_REPO_BASE as string | undefined) ??
  'vscode://file/D:/rpg/step8-cash/'

export function sourceHref(file: string, line: number): string {
  const base = REPO_BASE.endsWith('/') ? REPO_BASE : `${REPO_BASE}/`
  return `${base}${file}:${line}`
}

export type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: Architecture }

export async function loadArchitecture(): Promise<Architecture> {
  const response = await fetch(`${import.meta.env.BASE_URL}architecture.json`, {
    cache: 'no-store',
  })
  if (!response.ok) {
    throw new Error(`architecture.json: HTTP ${response.status}`)
  }
  const data = (await response.json()) as Architecture
  validate(data)
  return data
}

/** Минимальная проверка формы: битые ссылки ловятся генератором, здесь — структура. */
function validate(data: Architecture) {
  const required = ['meta', 'domains', 'nodes', 'edges', 'scenarios', 'owners', 'violations', 'source_locations']
  for (const key of required) {
    if (!(key in data)) throw new Error(`architecture.json: нет раздела «${key}»`)
  }
  const locations = new Set(data.source_locations.map((item) => item.id))
  const nodes = new Set(data.nodes.map((item) => item.id))
  const check = (id: string, where: string) => {
    if (!locations.has(id)) throw new Error(`architecture.json: ${where} ссылается на неизвестную локацию ${id}`)
  }
  for (const node of data.nodes) check(node.source_location, node.id)
  for (const edge of data.edges) {
    check(edge.source_location, edge.id)
    if (!nodes.has(edge.source) || !nodes.has(edge.target)) {
      throw new Error(`architecture.json: связь ${edge.id} ссылается на неизвестный узел`)
    }
  }
  for (const scenario of data.scenarios) {
    for (const variant of scenario.variants) {
      for (const step of variant.steps) {
        check(step.source_location, step.call)
        if (!nodes.has(step.node)) throw new Error(`architecture.json: шаг ${step.call} вне узлов`)
      }
    }
  }
  for (const owner of data.owners) {
    if (!nodes.has(owner.owner_node)) throw new Error(`architecture.json: владелец ${owner.id} вне узлов`)
  }
  for (const violation of data.violations) check(violation.source_location, violation.id)
}
