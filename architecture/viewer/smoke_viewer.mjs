/**
 * Рантайм-проверка viewer в настоящем браузере (headless Edge/Chrome).
 *
 * Запуск: node architecture/viewer/smoke_viewer.mjs http://127.0.0.1:5178/
 *
 * Проверяется то, что нельзя проверить статически:
 *   * страница монтируется и читает architecture.json (иначе был бы экран ошибки);
 *   * Cytoscape создал canvas ненулевого размера;
 *   * поиск сужает набор узлов;
 *   * фильтр по домену сужает набор узлов;
 *   * вкладка «Месячный ход» рисует Mermaid-диаграмму и таблицу шагов;
 *   * переключение варианта Python/Cython меняет число шагов;
 *   * вкладка «Владение и записи» показывает владельцев и таблицу нарушений.
 *
 * Важно: карта и её фильтры живут только на вкладке «Карта экономики», поэтому
 * проверки идут в порядке вкладок — иначе селекторы карты просто отсутствуют.
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

/** Число из счётчика «N / M узлов» и само N. */
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

    /**
     * Установить значение управляемого поля и дождаться, пока React его примет.
     *
     * Возвращает false, если поля нет: проверка тогда сообщает диагностикой,
     * а не падает посреди прогона.
     */
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

    const mapCounter = async () =>
      parseMapCounter(await evaluate(`document.querySelector('.toolbar .counter')?.textContent ?? ''`))

    await send('Page.enable')
    await send('Runtime.enable')
    await send('Page.navigate', { url })

    const mounted = await waitFor('#root nav.tabs', 25000)
    check(mounted, 'приложение не смонтировалось за 25 с')
    if (!mounted) throw new Error('нет интерфейса')
    await waitFor('#cy canvas', 15000)
    await sleep(900)

    // --- карта ------------------------------------------------------------
    const text = await evaluate('document.body.innerText')
    check(text.includes('Общая карта экономики'), 'нет боковой панели карты')
    check(!text.includes('Не удалось загрузить'), 'architecture.json не загрузился')
    check(text.includes('aad285e'), 'в шапке нет commit SHA')

    const canvases = await evaluate('document.querySelectorAll("#cy canvas").length')
    check(Number(canvases) >= 1, 'Cytoscape не создал canvas')
    const canvasWidth = await evaluate(`(() => {
      const canvas = document.querySelector('#cy canvas');
      return canvas ? canvas.width : 0;
    })()`)
    check(Number(canvasWidth) > 0, 'canvas карты нулевой ширины')

    const full = await mapCounter()
    check(full !== null, `счётчик карты не распознан: ${JSON.stringify(full)}`)
    if (!full) throw new Error('нет счётчика карты')
    check(full.total >= 50, `в карте мало узлов: ${full.total}`)

    // --- поиск ------------------------------------------------------------
    await setSearch('CashStore')
    const searched = await mapCounter()
    check(
      searched !== null && searched.visible > 0 && searched.visible < full.total,
      `поиск не сузил карту: ${JSON.stringify(searched)}`,
    )

    await setSearch('')
    const restored = await mapCounter()
    check(restored?.visible === full.total, `сброс поиска не вернул все узлы: ${JSON.stringify(restored)}`)

    // --- фильтр домена ----------------------------------------------------
    await setDomain('money')
    const byDomain = await mapCounter()
    check(
      byDomain !== null && byDomain.visible > 0 && byDomain.visible < full.total,
      `фильтр домена не сузил карту: ${JSON.stringify(byDomain)}`,
    )
    await setDomain('')

    // --- вкладка месяца ---------------------------------------------------
    await clickTab('Месячный ход')
    const sequenceText = await evaluate('document.body.innerText')
    check(sequenceText.includes('Шаги месяца'), 'нет таблицы шагов месяца')
    check(sequenceText.includes('Отличия от другого режима'), 'нет блока отличий режимов')
    check(sequenceText.includes('Python'), 'нет варианта Python')
    check(sequenceText.includes('Cython'), 'нет варианта Cython')

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
    const ownersText = await evaluate('document.body.innerText')
    for (const needle of [
      'Карта владения и записей',
      'Деньги счёта',
      'Заявка',
      'Проекты',
      'Результаты выпуска',
      'Отчёты и checkpoint',
      'Нарушения и зоны риска',
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
    'viewer smoke OK: карта + canvas, поиск, фильтр домена, sequence (Python/Cython), ownership',
  )
}

main().catch((error) => {
  console.error('SMOKE УПАЛ: ' + error.message)
  process.exit(1)
})
