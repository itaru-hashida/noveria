"""Включение multiprocessing под песочницей DSH.

Причина и разбор — `docs/SANDBOX_MULTIPROCESSING.md`. Коротко: токен процесса
write-restricted, поэтому канал, созданный с дефолтным дескриптором, не даёт
открыть себя на запись. Патч передаёт явный дескриптор «Everyone: полный
доступ». Импортировать и вызывать ДО создания пула и очередей: и Pool, и
ProcessPoolExecutor создают mp.Pipe для wakeup.
"""
import ctypes
from ctypes import wintypes

import multiprocessing.connection as _connection


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', wintypes.LPVOID),
                ('bInheritHandle', wintypes.BOOL)]


_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
_advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
_kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
_kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]

# Структуры обязаны жить всё время работы процесса: _winapi держит сырой адрес,
# и без ссылки сборщик освободит память, которую WinAPI ещё читает.
_KEEP = []


def _permissive_attributes():
    descriptor = wintypes.LPVOID()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            ctypes.c_wchar_p('D:(A;;GA;;;WD)'), 1, ctypes.byref(descriptor), None):
        raise OSError('SDDL не разобран, err=%d' % ctypes.get_last_error())
    attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    _KEEP.append((descriptor, attributes))
    # _winapi.CreateNamedPipe принимает int, а не ctypes-объект.
    return ctypes.cast(ctypes.byref(attributes), ctypes.c_void_p).value


def _pipe(duplex=True):
    """Копия multiprocessing.connection.Pipe; отличие только в дескрипторе."""
    import _winapi
    address = _connection.arbitrary_address('AF_PIPE')
    if duplex:
        openmode = _winapi.PIPE_ACCESS_DUPLEX
        access = _winapi.GENERIC_READ | _winapi.GENERIC_WRITE
        obsize, ibsize = _connection.BUFSIZE, _connection.BUFSIZE
    else:
        openmode = _winapi.PIPE_ACCESS_INBOUND
        access = _winapi.GENERIC_WRITE
        obsize, ibsize = 0, _connection.BUFSIZE
    h1 = _winapi.CreateNamedPipe(
        address, openmode | _winapi.FILE_FLAG_OVERLAPPED | _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE,
        _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT,
        1, obsize, ibsize, _winapi.NMPWAIT_WAIT_FOREVER, _permissive_attributes())
    h2 = _winapi.CreateFile(address, access, 0, _winapi.NULL, _winapi.OPEN_EXISTING,
                            _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
    _winapi.SetNamedPipeHandleState(h2, _winapi.PIPE_READMODE_MESSAGE, None, None)
    overlapped = _winapi.ConnectNamedPipe(h1, overlapped=True)
    _, err = overlapped.GetOverlappedResult(True)
    if err:
        raise OSError('ConnectNamedPipe: %d' % err)
    return (_connection.PipeConnection(h1, writable=duplex),
            _connection.PipeConnection(h2, readable=duplex))


def enable_multiprocessing():
    """Подменить Pipe. Повторный вызов безопасен."""
    if not getattr(_connection.Pipe, '_noveria_patched', False):
        _pipe._noveria_patched = True
        _connection.Pipe = _pipe
    return _connection.Pipe
