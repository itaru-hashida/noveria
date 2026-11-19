/**
 * Рантайм-проверка viewer в настоящем браузере (headless Edge/Chrome).
 *
 * Запуск: node architecture/viewer/smoke_viewer.mjs http://127.0.0.1:5178/
 *
 * Проверяется то, что нельзя проверить статически:
 *   * страница монтируется и читает architecture.json;
 *   * Cytoscape создал canvas ненулевого размера и на нём реально есть пиксели;
 *   * на карте видны разные цвета (значит связи не слились с фоном);
 *   * поиск и фильтр домена сужают набор узлов;
 *   * есть панель «как читать», расшифровка типов фигур и словарь;
 *   * вкладка месяца рисует Mermaid и таблицу, режимы отличаются;
 *   * вкладка владения показывает все шесть состояний и нарушения с пояснениями.
 *
 * Текст сравнивается в нижнем регистре: CSS поднимает заголовки в верхний
 * регистр, и innerText возвращает их уже такими.
 *
 * CDP используется напрямую: у viewer нет тестового сервера, а ставить
 * Playwright ради одной проверки — лишняя зависимость.
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

const url = process.argv[2] ?? 'http://127.0.0.1:5178/'
const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]
const port = 9339

const problems = []
const check = (condition, message) => {
  if (!condition) problems.push(message)
}

const sleep = (ms) => delay(ms)

function findBrowser() {
  return BROWSERS.find((candidate) => existsSync(candidate))
}

async function waitForTarget() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`)
      const list = await response.json()
      const page = list.find((item) => item.type === 'page')
      if (page) return page
    } catch {
      // браузер ещё поднимается
    }
    await sleep(250)
  }
  throw new Error('CDP не поднялся')
}

function connect(webSocketDebuggerUrl) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketDebuggerUrl)
    socket.onopen = () => resolve(socket)
    socket.onerror = () => reject(new Error('websocket не открылся'))
  })
}

function makeSender(socket) {
  let counter = 0
  const pending = new Map()
  const errors = []
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data)
    if (payload.id && pending.has(payload.id)) {
      const { resolve, reject } = pending.get(payload.id)
      pending.delete(payload.id)
      if (payload.error) reject(new Error(payload.error.message))
      else resolve(payload.result)
      return
    }
    if (payload.method === 'Runtime.exceptionThrown') {
      const details = payload.params.exceptionDetails
      errors.push(details.exception?.description ?? details.text)
    }
  }
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      counter += 1
      const id = counter
      pending.set(id, { resolve, reject })
      socket.send(JSON.stringify({ id, method, params }))
    })
  return { send, errors }
}

function parseMapCounter(value) {
  const match = String(value ?? '').match(/(\d+)\s*\/\s*(\d+)\s*узлов/)
  if (!match) return null
  return { visible: Number(match[1]), total: Number(match[2]) }
}

async function main() {
  const browser = findBrowser()
  if (!browser) {
    console.error('headless-браузер не найден; smoke пропущен')
    process.exit(2)
  }

  const child = spawn(
    browser,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-sandbox',
      '--disable-dev-shm-usage',
      // Размер окна важен: при 800×600 карта сжимается и подписи наезжают,
      // то есть проверка шла бы не на том, что видит человек.
      '--window-size=1600,1000',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${process.cwd()}/.tmp-e2e/smoke-profile`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  try {
    const target = await waitForTarget()
    const socket = await connect(target.webSocketDebuggerUrl)
    const { send, errors } = makeSender(socket)

    const evaluate = async (expression) => {
      const result = await send('Runtime.evaluate', {
        expression,
        returnByValue: true,
        awaitPromise: true,
      })
      if (result?.exceptionDetails) {
        throw new Error(
          `evaluate: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`,
        )
      }
      return result?.result?.value
    }

    const pageText = async () => String(await evaluate('document.body.innerText')).toLowerCase()

    const waitFor = async (selector, timeoutMs = 15000) => {
      const started = Date.now()
      while (Date.now() - started < timeoutMs) {
        const present = await evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)}))`)
        if (present) return true
        await sleep(250)
      }
      return false
    }

    const clickTab = async (label) => {
      await evaluate(`(() => {
        const button = [...document.querySelectorAll('nav.tabs button')]
          .find((item) => item.textContent.includes(${JSON.stringify(label)}));
        if (!button) throw new Error('нет вкладки ${label}');
        button.click();
        return true;
      })()`)
      await sleep(1600)
    }

    const setControlled = async (selector, value, eventName) => {
      const present = await waitFor(selector, 8000)
      if (!present) return false
      await evaluate(`(() => {
        const element = document.querySelector(${JSON.stringify(selector)});
        const prototype = element instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        setter.call(element, ${JSON.stringify(value)});
        element.dispatchEvent(new Event(${JSON.stringify(eventName)}, { bubbles: true }));
        return true;
      })()`)
      await sleep(1100)
      return true
    }

    const setSearch = (value) => setControlled('.toolbar input[type=search]', value, 'input')
    const setDomain = (value) => setControlled('.toolbar select', value, 'change')

    await send('Page.enable')
    await send('Runtime.enable')
    await send('Page.navigate', { url })

    const mounted = await waitFor('#root nav.tabs', 25000)
    check(mounted, 'приложение не смонтировалось за 25 с')
    if (!mounted) throw new Error('нет интерфейса')
    await waitFor('#cy canvas', 15000)
    await sleep(1200)

    // --- карта ------------------------------------------------------------
    const text = await pageText()
    check(text.includes('как читать карту'), 'нет панели «Как читать карту»')
    check(text.includes('типы фигур'), 'нет расшифровки типов фигур')
    check(text.includes('словарь'), 'нет словаря терминов')
    check(!text.includes('не удалось загрузить'), 'architecture.json не загрузился')

    const canvases = await evaluate('document.querySelectorAll("#cy canvas").length')
    check(Number(canvases) >= 1, 'Cytoscape не создал canvas')

    // Главная проверка: пиксели и разнообразие цветов. Cytoscape рисует
    // на нескольких слоях, поэтому берём слой с наибольшим числом пикселей.
    const canvasStats = await evaluate(`(() => {
      const list = [...document.querySelectorAll('#cy canvas')];
      let best = { painted: 0, variety: 0, width: 0, height: 0 };
      for (const canvas of list) {
        const ctx = canvas.getContext('2d');
        if (!ctx) continue;
        let data;
        try {
          data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        } catch (error) {
          continue;
        }
        let painted = 0;
        const colors = new Set();
        for (let i = 0; i < data.length; i += 4 * 7) {
          if (data[i + 3] < 12) continue;
          painted += 1;
          colors.add((data[i] >> 4) + '-' + (data[i + 1] >> 4) + '-' + (data[i + 2] >> 4));
        }
        if (painted > best.painted) {
          best = { painted, variety: colors.size, width: canvas.width, height: canvas.height };
        }
      }
      return best;
    })()`)
    check(canvasStats !== null, 'не удалось прочитать canvas карты')
    if (canvasStats) {
      check(canvasStats.painted > 150, `на карте почти ничего не нарисовано: ${canvasStats.painted}`)
      check(
        canvasStats.variety > 15,
        `на карте слишком мало разных цветов, связи могли слиться: ${canvasStats.variety}`,
      )
      check(canvasStats.width >= 900, `карта получила слишком узкий холст: ${canvasStats.width}px`)
    }

    const legendLines = await evaluate(`document.querySelectorAll('.edge-legend .legend-line').length`)
    check(Number(legendLines) >= 5, `в легенде связей мало видов: ${legendLines}`)

    // Регресс «видна только одна стрелка»: все связи должны доехать до графа.
    // Причина была в одинаковых id рёбер — Cytoscape оставлял одно из 73.
    const drawnEdges = await evaluate(`(() => {
      const container = document.getElementById('cy');
      const cy = container && container._cyreg && container._cyreg.cy;
      if (!cy) return { error: 'нет графа' };
      return {
        edges: cy.edges().length,
        kinds: [...new Set(cy.edges().map((edge) => edge.data('kind')))].length,
        colors: [...new Set(cy.edges().map((edge) => edge.style('line-color')))].length,
      };
    })()`)
    check(!drawnEdges.error, `не удалось прочитать граф: ${drawnEdges.error ?? ''}`)
    if (!drawnEdges.error) {
      check(
        drawnEdges.edges >= 70,
        `в графе потерялись связи: ${drawnEdges.edges}`,
      )
      check(drawnEdges.kinds >= 4, `в графе мало видов связей: ${drawnEdges.kinds}`)
      check(drawnEdges.colors >= 4, `связи почти одного цвета: ${drawnEdges.colors}`)
    }

    const shapeKinds = await evaluate(`document.querySelectorAll('.shape-glyph').length`)
    check(Number(shapeKinds) >= 5, `в легенде мало форм узлов: ${shapeKinds}`)

    const glossaryRows = await evaluate(`document.querySelectorAll('.glossary-row').length`)
    check(Number(glossaryRows) >= 8, `в словаре мало терминов: ${glossaryRows}`)

    const full = parseMapCounter(
      await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`),
    )
    check(full !== null, 'счётчик карты не распознан')
    if (!full) throw new Error('нет счётчика карты')
    check(full.total >= 50, `в карте мало узлов: ${full.total}`)

    const counterText = await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`)
    check(/связей/.test(counterText), `счётчик не показывает связи: ${counterText}`)

    // --- поиск ------------------------------------------------------------
    await setSearch('CashStore')
    const searched = parseMapCounter(
      await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`),
    )
    check(
      searched !== null && searched.visible > 0 && searched.visible < full.total,
      `поиск не сузил карту: ${JSON.stringify(searched)}`,
    )

    await setSearch('')
    const restored = parseMapCounter(
      await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`),
    )
    check(restored?.visible === full.total, 'сброс поиска не вернул все узлы')

    // --- фильтр домена ----------------------------------------------------
    await setDomain('money')
    const byDomain = parseMapCounter(
      await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`),
    )
    check(
      byDomain !== null && byDomain.visible > 0 && byDomain.visible < full.total,
      `фильтр домена не сузил карту: ${JSON.stringify(byDomain)}`,
    )
    await setDomain('')

    // --- вкладка месяца ---------------------------------------------------
    await clickTab('Месячный ход')
    const sequenceText = await pageText()
    check(sequenceText.includes('все шаги месяца по порядку'), 'нет таблицы шагов месяца')
    check(sequenceText.includes('чем отличаются два режима'), 'нет блока отличий режимов')
    check(sequenceText.includes('сначала — простыми словами'), 'нет вводного объяснения месяца')
    check(sequenceText.includes('python'), 'нет варианта Python')
    check(sequenceText.includes('cython'), 'нет варианта Cython')

    const svg = await evaluate(`document.querySelectorAll('.mermaid-host svg').length`)
    check(Number(svg) >= 1, 'Mermaid не отрисовал sequence-диаграмму')

    const pythonRows = Number(await evaluate(`document.querySelectorAll('table.grid tbody tr').length`))
    check(pythonRows > 20, `в таблице шагов мало строк: ${pythonRows}`)

    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.scroll-page button')]
        .find((item) => item.textContent.includes('Cython (native_mode'));
      if (!button) throw new Error('нет переключателя Cython');
      button.click();
      return true;
    })()`)
    await sleep(1600)
    const cythonRows = Number(await evaluate(`document.querySelectorAll('table.grid tbody tr').length`))
    check(cythonRows > 10, `в Cython-варианте мало шагов: ${cythonRows}`)
    check(cythonRows !== pythonRows, 'варианты месяца не отличаются по числу шагов')

    // --- вкладка владения -------------------------------------------------
    await clickTab('Владение и записи')
    const ownersText = await pageText()
    for (const needle of [
      'кто чем владеет',
      'деньги счёта',
      'склад',
      'заявка',
      'проекты',
      'результаты выпуска',
      'отчёты и checkpoint',
      'нарушения: что не так и почему это важно',
      'почему важно',
    ]) {
      check(ownersText.includes(needle), `нет раздела владения: ${needle}`)
    }
    const violationRows = Number(await evaluate(`document.querySelectorAll('table.grid tbody tr').length`))
    check(violationRows >= 15, `в таблице нарушений мало строк: ${violationRows}`)

    check(errors.length === 0, `ошибки в консоли: ${errors.slice(0, 3).join(' | ')}`)
    socket.close()
  } finally {
    child.kill()
  }

  if (problems.length) {
    console.error('SMOKE НЕ ПРОШЁЛ:')
    for (const message of problems) console.error(' - ' + message)
    process.exit(1)
  }
  console.log(
    'viewer smoke OK: карта с видимыми связями, поиск, фильтр домена, sequence (Python/Cython), ownership с объяснениями',
  )
}

main().catch((error) => {
  console.error('SMOKE УПАЛ: ' + error.message)
  process.exit(1)
})
