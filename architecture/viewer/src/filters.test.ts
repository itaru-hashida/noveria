/**
 * Проверка фильтров карты на реальном architecture.json.
 *
 * Запуск: node --test architecture/viewer/src/filters.test.ts
 * (Node 24 исполняет TypeScript напрямую, отдельная сборка не нужна.)
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { Architecture } from './types.ts'
import {
  callbackNodeIds,
  directWriteNodeIds,
  emptyFilters,
  matchesQuery,
  ownerNodeIds,
  visibleNodeIds,
} from './filters.ts'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '../../..')
const data = JSON.parse(
  readFileSync(join(root, 'architecture/architecture.json'), 'utf8'),
) as Architecture

test('без фильтров видны все узлы', () => {
  const visible = visibleNodeIds(data, emptyFilters)
  assert.equal(visible.size, data.nodes.length)
})

test('поиск по имени класса находит узел', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, query: 'CashStore' })
  assert.ok(visible.has('money.cash_store'))
  assert.ok(visible.has('money.cash_store_python'))
  assert.ok(visible.size < data.nodes.length)
})

test('поиск по файлу и по строке находит узел', () => {
  const byFile = visibleNodeIds(data, { ...emptyFilters, query: 'cash_store.pyx' })
  assert.ok(byFile.has('money.cash_store'))

  const record = data.source_locations.find((item) => item.id === 'loc-0001')
  assert.ok(record)
  const byLine = visibleNodeIds(data, {
    ...emptyFilters,
    query: `${record!.file}:${record!.line}`,
  })
  assert.ok(byLine.size >= 1)
})

test('поиск без совпадений даёт пустой набор', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, query: 'zzz-нет-такого-символа-zzz' })
  assert.equal(visible.size, 0)
})

test('несколько слов в поиске работают как AND', () => {
  const single = visibleNodeIds(data, { ...emptyFilters, query: 'CashStore' })
  const double = visibleNodeIds(data, { ...emptyFilters, query: 'CashStore python' })
  assert.ok(double.size > 0)
  assert.ok(double.size <= single.size)
})

test('фильтр домена оставляет только этот домен', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, domain: 'money' })
  assert.ok(visible.size > 0)
  for (const id of visible) {
    const node = data.nodes.find((item) => item.id === id)
    assert.equal(node?.domain, 'money')
  }
})

test('фильтр Cython оставляет только cython-узлы', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, languages: new Set(['cython']) })
  assert.ok(visible.size > 0)
  for (const id of visible) {
    const node = data.nodes.find((item) => item.id === id)
    assert.equal(node?.language, 'cython')
  }
})

test('фильтр Python оставляет только python-узлы', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, languages: new Set(['python']) })
  assert.ok(visible.size > 0)
  for (const id of visible) {
    const node = data.nodes.find((item) => item.id === id)
    assert.equal(node?.language, 'python')
  }
})

test('Python + Cython вместе равны отсутствию фильтра', () => {
  const visible = visibleNodeIds(data, { ...emptyFilters, languages: new Set(['python', 'cython']) })
  assert.equal(visible.size, data.nodes.length)
})

test('подсветка владельцев показывает только владельцев', () => {
  const owners = ownerNodeIds(data)
  const visible = visibleNodeIds(data, { ...emptyFilters, ownersOnly: true })
  assert.equal(visible.size, owners.size)
  for (const id of visible) assert.ok(owners.has(id))
})

test('подсветка прямых записей показывает только узлы нарушений', () => {
  const targets = directWriteNodeIds(data)
  const visible = visibleNodeIds(data, { ...emptyFilters, writesOnly: true })
  assert.equal(visible.size, targets.size)
  assert.ok(visible.size > 0)
})

test('подсветка callbacks пересекается только с callback-узлами', () => {
  const targets = callbackNodeIds(data)
  const visible = visibleNodeIds(data, { ...emptyFilters, callbacksOnly: true })
  assert.equal(visible.size, targets.size)
  assert.ok(visible.size > 0)
})

test('фильтры комбинируются пересечением', () => {
  const cythonOnly = visibleNodeIds(data, { ...emptyFilters, languages: new Set(['cython']) })
  const combined = visibleNodeIds(data, {
    ...emptyFilters,
    languages: new Set(['cython']),
    domain: 'money',
  })
  assert.ok(combined.size <= cythonOnly.size)
  for (const id of combined) {
    const node = data.nodes.find((item) => item.id === id)
    assert.equal(node?.language, 'cython')
    assert.equal(node?.domain, 'money')
  }
})

test('matchesQuery пустая строка означает «показывать всё»', () => {
  for (const node of data.nodes) assert.equal(matchesQuery(data, node, '   '), true)
})
