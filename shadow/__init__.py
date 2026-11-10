"""Shadow-прототип агентного планирования.

Запускается рядом с V3, ничего не меняет в движке и базе: читает живой мир,
строит неизменяемый снимок, планирует предприятия параллельно, сводит заявки
через FIFO-очередь и пишет отчёт.

Схема (docs/AGENT_PLANNING_ARCHITECTURE.md):
    parent:
      snapshot -> shared_memory (numpy, read-only)
      ProcessPoolExecutor: только plan()
      callback -> IntentQueue.put()
      MarketExecutor thread: get -> reserve -> execute -> Outcome
"""
