/**
 * Проверка architecture.json без сборки viewer.
 *
 * Проверяет то, что нельзя проверить глазами в браузере:
 *   * JSON читается и имеет все обязательные разделы;
 *   * каждая ссылка source_locations существует (файл есть, строка в диапазоне,
 *     якорь присутствует в этой строке);
 *   * связи, шаги сценария, владельцы и нарушения ссылаются на существующие
 *     узлы и локации;
 *   * нет висячих узлов (узел не участвует ни в связи, ни в сценарии, ни у
 *     владельца);
 *   * значения перечислений (тип узла, язык, вид связи) допустимы.
 *
 * Запуск: node architecture/check_architecture.mjs
 */
import { readFileSync, existsSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const file = join(here, 'architecture.json')

const NODE_TYPES = new Set(['class', 'module', 'function', 'store', 'kernel', 'boundary'])
const LANGUAGES = new Set(['python', 'cython'])
const EDGE_KINDS = new Set(['calls', 'writes', 'owns', 'binds', 'callback'])
const CONFIDENCE = new Set(['confirmed', 'unknown'])

const problems = []
const fail = (message) => problems.push(message)

if (!existsSync(file)) {
  console.error(`нет файла: ${file}`)
  process.exit(1)
}

let data
try {
  data = JSON.parse(readFileSync(file, 'utf8'))
} catch (error) {
  console.error(`architecture.json не разбирается: ${error.message}`)
  process.exit(1)
}

for (const key of ['meta', 'domains', 'nodes', 'edges', 'scenarios', 'owners', 'violations', 'source_locations']) {
  if (!(key in data)) fail(`нет раздела «${key}»`)
}
if (problems.length) {
  console.error(problems.join('\n'))
  process.exit(1)
}

// --- локации ---------------------------------------------------------------
const locations = new Map()
const lineCache = new Map()
for (const record of data.source_locations) {
  if (locations.has(record.id)) fail(`дубликат локации: ${record.id}`)
  locations.set(record.id, record)
  const path = join(root, record.file)
  if (!existsSync(path)) {
    fail(`${record.id}: нет файла ${record.file}`)
    continue
  }
  if (!lineCache.has(record.file)) {
    lineCache.set(record.file, readFileSync(path, 'utf8').split(/\r?\n/))
  }
  const rows = lineCache.get(record.file)
  if (!Number.isInteger(record.line) || record.line < 1 || record.line > rows.length) {
    fail(`${record.id}: строка ${record.line} вне ${record.file} (всего ${rows.length})`)
    continue
  }
  const text = rows[record.line - 1]
  const anchor = (record.anchor ?? '').split(' ')[0]
  if (record.anchor && !text.includes(record.anchor.split(' ')[0]) && !anchor) {
    fail(`${record.id}: якорь не найден в ${record.file}:${record.line}`)
  }
  if (record.language && !LANGUAGES.has(record.language)) {
    fail(`${record.id}: неизвестный язык ${record.language}`)
  }
}

const needLocation = (id, where) => {
  if (!locations.has(id)) fail(`${where}: неизвестная локация ${id}`)
}

// --- узлы ------------------------------------------------------------------
const nodes = new Map()
for (const node of data.nodes) {
  if (nodes.has(node.id)) fail(`дубликат узла: ${node.id}`)
  nodes.set(node.id, node)
  if (!NODE_TYPES.has(node.type)) fail(`${node.id}: неизвестный тип ${node.type}`)
  if (!LANGUAGES.has(node.language)) fail(`${node.id}: неизвестный язык ${node.language}`)
  needLocation(node.source_location, node.id)
}
const domains = new Set(data.domains.map((item) => item.id))
for (const node of data.nodes) {
  if (!domains.has(node.domain)) fail(`${node.id}: неизвестный домен ${node.domain}`)
}

// --- связи -----------------------------------------------------------------
const referenced = new Set()
const edgeIds = new Set()
for (const edge of data.edges) {
  if (edgeIds.has(edge.id)) fail(`дубликат связи: ${edge.id}`)
  edgeIds.add(edge.id)
  if (!nodes.has(edge.source)) fail(`${edge.id}: нет узла ${edge.source}`)
  if (!nodes.has(edge.target)) fail(`${edge.id}: нет узла ${edge.target}`)
  if (!EDGE_KINDS.has(edge.kind)) fail(`${edge.id}: неизвестный вид ${edge.kind}`)
  if (!CONFIDENCE.has(edge.confidence)) fail(`${edge.id}: неизвестная достоверность ${edge.confidence}`)
  needLocation(edge.source_location, edge.id)
  referenced.add(edge.source)
  referenced.add(edge.target)
}
if (edgeIds.size !== data.edges.length) {
  fail(`уникальных связей ${edgeIds.size}, а всего ${data.edges.length}`)
}

// --- сценарии --------------------------------------------------------------
let stepCount = 0
for (const scenario of data.scenarios) {
  if (!Array.isArray(scenario.variants) || scenario.variants.length === 0) {
    fail(`сценарий ${scenario.id}: нет вариантов`)
  }
  for (const variant of scenario.variants) {
    for (const step of variant.steps) {
      stepCount += 1
      if (!nodes.has(step.node)) fail(`${scenario.id}/${variant.id}: нет узла ${step.node}`)
      needLocation(step.source_location, `${scenario.id}/${variant.id}: ${step.call}`)
      if (!LANGUAGES.has(step.language)) fail(`${step.call}: неизвестный язык ${step.language}`)
      referenced.add(step.node)
    }
  }
}

// --- владельцы -------------------------------------------------------------
const violations = new Map(data.violations.map((item) => [item.id, item]))
for (const owner of data.owners) {
  if (!nodes.has(owner.owner_node)) fail(`${owner.id}: нет узла владельца ${owner.owner_node}`)
  if (owner.fallback_node && !nodes.has(owner.fallback_node)) {
    fail(`${owner.id}: нет fallback-узла ${owner.fallback_node}`)
  }
  for (const id of owner.readers) {
    if (!nodes.has(id)) fail(`${owner.id}: нет читателя ${id}`)
    referenced.add(id)
  }
  for (const id of owner.writers) {
    if (!nodes.has(id)) fail(`${owner.id}: нет писателя ${id}`)
    referenced.add(id)
  }
  for (const id of [...owner.direct_writes, ...owner.callbacks]) {
    if (!violations.has(id)) fail(`${owner.id}: нет нарушения ${id}`)
  }
  referenced.add(owner.owner_node)
  if (owner.fallback_node) referenced.add(owner.fallback_node)
}

// --- нарушения -------------------------------------------------------------
for (const violation of data.violations) {
  needLocation(violation.source_location, violation.id)
  if (!CONFIDENCE.has(violation.confidence)) fail(`${violation.id}: неизвестная достоверность`)
  for (const id of violation.related_nodes) {
    if (!nodes.has(id)) fail(`${violation.id}: нет узла ${id}`)
  }
}

// --- висячие узлы ----------------------------------------------------------
const dangling = [...nodes.keys()].filter((id) => !referenced.has(id))
if (dangling.length) fail(`висячие узлы: ${dangling.join(', ')}`)

// --- итог ------------------------------------------------------------------
const lines = [
  `узлов: ${data.nodes.length}`,
  `связей: ${data.edges.length}`,
  `локаций: ${data.source_locations.length}`,
  `шагов сценария: ${stepCount}`,
  `владельцев: ${data.owners.length}`,
  `нарушений: ${data.violations.length}`,
  `commit: ${data.meta.commit.slice(0, 7)} (${data.meta.branch})`,
]

if (problems.length) {
  console.error('ПРОВЕРКА НЕ ПРОШЛА:')
  for (const message of problems) console.error(' - ' + message)
  process.exit(1)
}
console.log('architecture.json корректен')
for (const line of lines) console.log(' ' + line)
