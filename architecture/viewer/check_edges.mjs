/**
 * Проверка, что на карте действительно видны стрелки, а не одна.
 *
 * Запуск: node architecture/viewer/check_edges.mjs http://127.0.0.1:5178/
 *
 * Считает, сколько рёбер Cytoscape реально отрисовал и сколько цветов они
 * дают на холсте. Если цвет ребра совпадает с фоном, пикселей будет мало —
 * именно так выглядела жалоба «видна только одна стрелка».
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

const url = process.argv[2] ?? 'http://127.0.0.1:5178/'
const BRAVE = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
]
const port = 9343
const sleep = (ms) => delay(ms)

const browser = BRAVE.find((candidate) => existsSync(candidate))
if (!browser) {
  console.error('headless-браузер не найден')
  process.exit(2)
}

const child = spawn(
  browser,
  [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    '--window-size=1600,1000',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${process.cwd()}/.tmp-e2e/edges-profile`,
    'about:blank',
  ],
  { stdio: 'ignore' },
)

async function waitForTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`)
      const list = await response.json()
      const page = list.find((item) => item.type === 'page')
      if (page) return page
    } catch {
      // ещё поднимается
    }
    await sleep(250)
  }
  throw new Error('CDP не поднялся')
}

try {
  const target = await waitForTarget()
  const socket = await new Promise((resolve, reject) => {
    const ws = new WebSocket(target.webSocketDebuggerUrl)
    ws.onopen = () => resolve(ws)
    ws.onerror = () => reject(new Error('websocket'))
  })

  let counter = 0
  const pending = new Map()
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data)
    if (payload.id && pending.has(payload.id)) {
      const { resolve } = pending.get(payload.id)
      pending.delete(payload.id)
      resolve(payload.result)
    }
  }
  const send = (method, params = {}) =>
    new Promise((resolve) => {
      counter += 1
      const id = counter
      pending.set(id, { resolve })
      socket.send(JSON.stringify({ id, method, params }))
    })

  const evaluate = async (expression) => {
    const result = await send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    })
    if (result?.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ?? 'ошибка evaluate')
    }
    return result?.result?.value
  }

  await send('Page.enable')
  await send('Runtime.enable')
  await send('Page.navigate', { url })
  await sleep(6000)

  const report = await evaluate(`(() => {
    const container = document.getElementById('cy');
    const cy = container && container._cyreg && container._cyreg.cy;
    if (!cy) return { error: 'нет cytoscape-инстанса' };

    const edges = cy.edges().filter((edge) => !edge.hasClass('filtered'));
    const byKind = {};
    const byColor = {};
    edges.forEach((edge) => {
      const kind = edge.data('kind');
      byKind[kind] = (byKind[kind] || 0) + 1;
      const color = edge.style('line-color');
      byColor[color] = (byColor[color] || 0) + 1;
    });

    // Сколько пикселей похоже на линии: считаем только плотные слои canvas.
    let painted = 0;
    let distinct = 0;
    for (const canvas of [...document.querySelectorAll('#cy canvas')]) {
      const ctx = canvas.getContext('2d');
      if (!ctx) continue;
      let data;
      try { data = ctx.getImageData(0, 0, canvas.width, canvas.height).data; }
      catch (error) { continue; }
      const seen = new Set();
      let local = 0;
      for (let i = 0; i < data.length; i += 4 * 5) {
        if (data[i + 3] < 24) continue;
        local += 1;
        seen.add((data[i] >> 3) + '-' + (data[i + 1] >> 3) + '-' + (data[i + 2] >> 3));
      }
      if (local > painted) { painted = local; distinct = seen.size; }
    }

    return {
      edges: edges.length,
      byKind,
      colors: Object.keys(byColor).length,
      colorMap: byColor,
      painted,
      distinct,
      zoom: Math.round(cy.zoom() * 100) / 100,
    };
  })()`)

  if (report.error) {
    console.error('НЕ УДАЛОСЬ ИЗМЕРИТЬ: ' + report.error)
    process.exit(1)
  }

  console.log(`рёбер в графе: ${report.edges}`)
  console.log('по видам: ' + JSON.stringify(report.byKind))
  console.log(`разных цветов у рёбер: ${report.colors}`)
  console.log('цвета: ' + JSON.stringify(report.colorMap))
  console.log(`пикселей на карте: ${report.painted}, разных оттенков: ${report.distinct}`)
  console.log(`масштаб: ${report.zoom}`)

  socket.close()

  const problems = []
  if (report.edges < 60) problems.push(`в графе мало рёбер: ${report.edges}`)
  if (report.colors < 3) problems.push(`рёбра почти одного цвета: ${report.colors}`)
  if (report.painted < 400) problems.push(`на холсте мало пикселей: ${report.painted}`)
  if (report.distinct < 12) problems.push(`на холсте мало оттенков: ${report.distinct}`)

  if (problems.length) {
    console.error('ПРОВЕРКА СВЯЗЕЙ НЕ ПРОШЛА:')
    for (const item of problems) console.error(' - ' + item)
    process.exitCode = 1
  } else {
    console.log('связи видны и различаются по цвету')
  }
} finally {
  child.kill()
}
