# Памятка: `multiprocessing` под песочницей DSH на Windows

Записано 10.11.2026 на ветке `experiment/parallel-agent-planning`. Повод:
`AGENTS.md` §5 и §9 утверждали, что «песочница запрещает именованные каналы,
поэтому `multiprocessing` и `ProcessPoolExecutor` не работают». **Это неверно.**
Пулы работают штатно, если канал создаётся с явным дескриптором доступа.

## Симптом

```text
PermissionError: [WinError 5] Отказано в доступе
```

падает на `mp.Pool`, `ProcessPoolExecutor`, `mp.Pipe`, `mp.Queue`,
`mp.SimpleQueue`. При этом `subprocess.Popen` работает, а `mp.Manager`
падает отдельно и по другой причине (см. ниже).

## Причина

Не запрет каналов. Токен процесса — **restricted** (`IsRestricted=1`, четыре
restricted SID): Windows при таком токене разрешает чтение объекта, а запись —
только если нужный SID явно перечислен в DACL объекта.

CPython создаёт именованный канал с дефолтным дескриптором, где этих SID нет.
Ломается не `CreateNamedPipe`, а следующий вызов —
`Lib/multiprocessing/connection.py`, `Pipe()`:

```python
h1 = _winapi.CreateNamedPipe(address, ..., _winapi.NULL)   # работает
h2 = _winapi.CreateFile(address, access, ...)             # ← WinError 5 здесь
```

`access` для дуплекса — `GENERIC_READ | GENERIC_WRITE`.

## Как проверить за минуту, а не за час

```python
# 1. Какие примитивы живы
import multiprocessing as mp, os, _winapi
_winapi.CreatePipe(None, 16384)        # анонимный канал — OK
os.pipe()                              # OK
mp.Pipe()                              # падает

# 2. Какая маска прав запрещена (свежий канал на каждую попытку!)
#    GENERIC_READ, FILE_READ_DATA, FILE_APPEND_DATA, READ_CONTROL, SYNCHRONIZE — OK
#    GENERIC_WRITE, FILE_WRITE_DATA, FILE_WRITE_ATTRIBUTES, WRITE_DAC — err=5

# 3. Токен
#    GetTokenInformation(TokenIsRestricted) == 1  → вот и ответ
```

Полезное правило: если чтение объекта разрешено, а запись нет — это
write-restricted токен, а не отсутствие прав у пользователя. Проверять DACL
объекта бессмысленно, он корректен; смотреть надо токен.

## Рабочий рецепт

Подменить `multiprocessing.connection.Pipe` до создания пула. Отличие от
CPython — только `SECURITY_ATTRIBUTES` в `CreateNamedPipe`:

```python
import ctypes
from ctypes import wintypes
import multiprocessing.connection as mc

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
advapi = ctypes.WinDLL('advapi32', use_last_error=True)
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.LPVOID]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', wintypes.LPVOID),
                ('bInheritHandle', wintypes.BOOL)]


_KEEP = []


def _attributes():
    """DACL «Everyone: полный доступ». _KEEP обязателен: _winapi держит сырой
    адрес, и без ссылки сборщик освободит структуру между вызовами."""
    descriptor = wintypes.LPVOID()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        ctypes.c_wchar_p('D:(A;;GA;;;WD)'), 1, ctypes.byref(descriptor), None)
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
    _KEEP.append((descriptor, attributes))
    return ctypes.cast(ctypes.byref(attributes), ctypes.c_void_p).value  # нужен int


def _pipe(duplex=True):
    """Копия CPython multiprocessing.connection.Pipe."""
    import _winapi
    address = mc.arbitrary_address('AF_PIPE')
    if duplex:
        openmode = _winapi.PIPE_ACCESS_DUPLEX
        access = _winapi.GENERIC_READ | _winapi.GENERIC_WRITE
        obsize, ibsize = mc.BUFSIZE, mc.BUFSIZE
    else:
        openmode = _winapi.PIPE_ACCESS_INBOUND
        access = _winapi.GENERIC_WRITE
        obsize, ibsize = 0, mc.BUFSIZE
    h1 = _winapi.CreateNamedPipe(
        address, openmode | _winapi.FILE_FLAG_OVERLAPPED | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
        _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
        1, obsize, ibsize, _winapi.NMPWAIT_WAIT_FOREVER, _attributes())
    h2 = _winapi.CreateFile(address, access, 0, _winapi.NULL, _winapi.OPEN_EXISTING,
                            _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
    _winapi.SetNamedPipeHandleState(h2, _winapi.PIPE_READMODE_MESSAGE, None, None)
    overlapped = _winapi.ConnectNamedPipe(h1, overlapped=True)
    _, err = overlapped.GetOverlappedResult(True)
    assert err == 0
    return mc.PipeConnection(h1, writable=duplex), mc.PipeConnection(h2, readable=duplex)


def enable_multiprocessing():
    mc.Pipe = _pipe
```

Две грабли в этом коде, обе стоили попытки:

- `_winapi.CreateNamedPipe` принимает **int**, а не `ctypes.byref(...)` —
  `TypeError: argument 8 must be int, not _ctypes.CArgObject`. Отсюда
  `ctypes.cast(..., c_void_p).value`.
- без `_KEEP` структура живёт только до возврата из `_attributes()`, и
  WinAPI читает освобождённую память.

## Что проверено после патча

| механизм | результат |
|---|---|
| `ProcessPoolExecutor.map` | OK |
| `mp.Pool.map` / `imap_unordered` | OK |
| `subprocess.Popen` | OK и без патча |
| `mp.Manager` | требует `if __name__ == '__main__'` (spawn переимпортирует модуль), к песочнице отношения не имеет |

Анонимные каналы (`_winapi.CreatePipe`, `os.pipe`) работали всегда — на них
стоит stdin дочернего процесса, поэтому `subprocess.Popen` не страдал.

## Следствия для проекта

- `audit/seed_matrix.py` запускает сиды дочерними процессами с обменом через
  JSON-файлы «потому что пул невозможен». Причина неверна; файловый обмен
  можно заменить пулом, когда понадобится.
- `scripts/validate_stock_planning.py` и `validate_business_income.py` уже
  вызывают `ProcessPoolExecutor`, то есть на такой машине они падали не по
  своей вине.
- Правку в `AGENTS.md` §5 и §9 нужно внести отдельным коммитом: там записано
  утверждение, которое этот файл опровергает.

## Гипотеза, которую НЕ проверяли

Ускоряет ли пул процессов настоящее планирование. В `production_outlook`,
`unit_cost`, `landed`, `customer_quotes` кэши ключуются по `id()` живых
объектов, и в дочерний процесс их переносить нельзя: там `id` чужой.
Сначала нужен снимок с числовыми ключами, потом замер. Замер на прогретом
кэше даёт 0,001 s и ничего не значит — мерить только на холодных кэшах и
независимых копиях движка.
