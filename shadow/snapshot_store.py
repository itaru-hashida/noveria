"""Снимок в shared_memory: запись в родителе, чтение в worker-ах.

Одна плоская область. Родитель пишет массивы и отдаёт worker-ам только
`Layout` (имена/типы/формы/смещения) и имя области. Dict не передаётся.
"""
import numpy as np
from multiprocessing import shared_memory

from shadow.agent_snapshot import LAYOUT_ALIGN


class SharedSnapshot:
    """Владелец области. Освобождается после пула и исполнителя."""

    def __init__(self, arrays, layout, size, name):
        self.arrays = arrays
        self.layout = layout
        self.size = size
        self.name = name
        self._shm = None

    @classmethod
    def create(cls, arrays, layout, size, name=None):
        shm = shared_memory.SharedMemory(create=True, size=max(size, 1), name=name)
        try:
            for entry_name, _dtype, _shape, offset in layout.entries:
                array = arrays[entry_name]
                buffer = np.ndarray(array.shape, dtype=array.dtype, buffer=shm.buf,
                                    offset=offset)
                buffer[:] = array[:]
            # Снимок неизменяем: агенты только читают.
            for entry_name, _dtype, _shape, offset in layout.entries:
                array = arrays[entry_name]
                view = np.ndarray(array.shape, dtype=array.dtype, buffer=shm.buf, offset=offset)
                view.flags.writeable = False
        except Exception:
            shm.close()
            shm.unlink()
            raise
        instance = cls(arrays, layout, size, shm.name)
        instance._shm = shm
        return instance

    def release(self):
        if self._shm is not None:
            self._shm.close()
            self._shm.unlink()
            self._shm = None


def read_arrays(name, layout):
    """В worker-е: подключиться и получить read-only массивы."""
    shm = shared_memory.SharedMemory(name=name)
    arrays = {}
    for entry_name, dtype, shape, offset in layout.entries:
        array = np.ndarray(shape, dtype=np.dtype(dtype), buffer=shm.buf, offset=offset)
        array.flags.writeable = False
        arrays[entry_name] = array
    return shm, arrays
