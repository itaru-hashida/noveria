/**
 * Измерение перекрытия подписей узлов на карте.
 *
 * Запуск: node architecture/viewer/check_overlap.mjs http://127.0.0.1:5178/
 *
 * Берёт реальные ограничивающие рамки, которые Cytoscape посчитал для каждой
 * подписи, и считает пары, которые пересекаются. Это объективная проверка
 * жалобы «текст фигурок налазит друг на друга»: глазами такое не измерить, а
 * через API — можно.
 *
 * Рамки доменов исключены: узел внутри своей рамки пересекается с ней по
 * определению, это не наложение подписей.
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

const url = process.argv[2] ?? 'http://127.0.0.1:5178/'
const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
]
const port = 9341
const sleep = (ms) => delay(ms)

const browser = BROWSERS.find((candidate) => existsSync(candidate))
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
    `--user-data-dir=${process.cwd()}/.tmp-e2e/overlap-profile`,
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
    if (!container) return { error: 'нет контейнера #cy' };
    const cy = container._cyreg && container._cyreg.cy;
    if (!cy) return { error: 'нет cytoscape-инстанса' };

    // Берём только реальные узлы: рамки доменов — подложки, а не узлы карты.
    const boxes = [];
    cy.nodes().forEach((node) => {
      if (node.data('isFrame')) return;
      if (node.hasClass('filtered') || node.hidden()) return;
      const labelBox = node.boundingBox({ includeLabels: true, includeNodes: false, includeOverlays: false });
      const nodeBox = node.boundingBox({ includeLabels: false, includeNodes: true, includeOverlays: false });
      boxes.push({
        id: node.id(),
        label: String(node.data('short') || node.data('label')).split('\\n')[0],
        x1: labelBox.x1, y1: labelBox.y1, x2: labelBox.x2, y2: labelBox.y2,
        nx1: nodeBox.x1, ny1: nodeBox.y1, nx2: nodeBox.x2, ny2: nodeBox.y2,
      });
    });

    const overlapArea = (a, b) => {
      const w = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
      const h = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
      if (w <= 0 || h <= 0) return 0;
      return w * h;
    };

    const labelHits = [];
    const nodeHits = [];
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i];
        const b = boxes[j];
        const area = overlapArea(a, b);
        if (area > 0) labelHits.push({ a: a.label, b: b.label, area: Math.round(area) });
        const na = { x1: a.nx1, y1: a.ny1, x2: a.nx2, y2: a.ny2 };
        const nb = { x1: b.nx1, y1: b.ny1, x2: b.nx2, y2: b.ny2 };
        if (overlapArea(na, nb) > 0) nodeHits.push([a.label, b.label]);
      }
    }

    return {
      nodes: boxes.length,
      labelOverlaps: labelHits.length,
      worst: labelHits.sort((l, r) => r.area - l.area).slice(0, 5),
      nodeOverlaps: nodeHits.length,
      nodeSamples: nodeHits.slice(0, 5),
      zoom: Math.round(cy.zoom() * 100) / 100,
      extent: cy.extent(),
    };
  })()`)

  if (report.error) {
    console.error('НЕ УДАЛОСЬ ИЗМЕРИТЬ: ' + report.error)
    process.exit(1)
  }

  console.log(`узлов на карте: ${report.nodes}`)
  console.log(`перекрытий подписей: ${report.labelOverlaps}`)
  console.log(`перекрытий фигур: ${report.nodeOverlaps}`)
  console.log(`масштаб: ${report.zoom}`)
  if (report.worst.length) {
    console.log('худшие пары подписей:')
    for (const item of report.worst) console.log(`  ${item.a} × ${item.b} — ${item.area}px²`)
  }
  if (report.nodeSamples.length) {
    console.log('накладывающиеся фигуры:')
    for (const pair of report.nodeSamples) console.log(`  ${pair[0]} × ${pair[1]}`)
  }

  socket.close()

  // Подписи не должны пересекаться: две-три строки текста под фигурой при
  // плотной укладке сливаются в кашу, и карта перестаёт читаться.
  const problems = []
  if (report.nodes < 70) problems.push(`на карте мало узлов: ${report.nodes}`)
  if (report.labelOverlaps > 0) problems.push(`перекрываются подписи: ${report.labelOverlaps}`)
  if (report.nodeOverlaps > 0) problems.push(`перекрываются фигуры: ${report.nodeOverlaps}`)

  if (problems.length) {
    console.error('ПРОВЕРКА НАЛОЖЕНИЙ НЕ ПРОШЛА:')
    for (const item of problems) console.error(' - ' + item)
    process.exitCode = 1
  } else {
    console.log('подписи и фигуры не накладываются')
  }
} finally {
  child.kill()
}
