from core.events import EventBus


def test_priorities_fifo_subscribers_and_errors():
    bus = EventBus()
    calls = []
    bus.subscribe('test',lambda e:calls.append(('late',e)),priority=10)
    bus.subscribe('test',lambda e:1/0,priority=1)
    bus.subscribe('test',lambda e:calls.append(('first',e)),priority=1)
    bus.subscribe('test',lambda e:calls.append(('second',e)),priority=1)
    for priority,value in [(10,'background'),(5,'normal-a'),(1,'critical'),(5,'normal-b')]:
        bus.publish('test',value,priority=priority)
    bus.process_queue()
    assert calls == [(handler,value) for value in ('critical','normal-a','normal-b','background')
                     for handler in ('first','second','late')]


def test_published_during_processing():
    bus = EventBus()
    calls = []
    def handle(value):
        calls.append(value)
        if value == 'initial':
            bus.publish('test','nested',priority=1)
            bus.process_queue()
    bus.subscribe('test',handle)
    bus.publish('test','initial')
    bus.publish('test','waiting',priority=10)
    bus.process_queue()
    assert calls == ['initial','nested','waiting']
