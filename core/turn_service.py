"""Месячный цикл: обязательные фазы внутри единой транзакции."""
import logging
import time
from uuid import uuid4

from core.integrity import validate_world

log=logging.getLogger(__name__)


class TurnService:
    def __init__(self,service):
        self.service=service; self.db=service.db
        self.phases=[('commands',self._commands),('economy',self._economy),('integrity',self._integrity),('snapshot',self._snapshot)]

    def _economy(self, ctx):
        from modules.economy import monthly_phase
        return monthly_phase(self.service, ctx)

    def register_phase(self,name,handler,before='snapshot'):
        if any(n==name for n,_ in self.phases): raise ValueError('Фаза уже зарегистрирована')
        position=next(i for i,(n,_) in enumerate(self.phases) if n==before)
        self.phases.insert(position,(name,handler))

    def _commands(self,ctx):
        commands=self.db.pending_commands()
        for command in commands:
            p=command['payload']
            if command['kind']!='set_social': raise ValueError('Неизвестная команда хода')
            entity=self.db.get_entity(p['entity_id'])
            if entity['kind']!='SocialGroup' or p['parameter'] not in ('population','loyalty','welfare','influence'):
                raise ValueError('Некорректная социальная команда')
            old=self.db.update_parameter(p['entity_id'],p['parameter'],p['value'])
            event=self.service.event('parameter_changed',dict(parameter=p['parameter'],old=old,new=p['value']),
                [dict(id=p['entity_id'],role='subject')],source='turn',action_id=ctx['command_id'],summary='Выполнено социальное распоряжение')
            ctx['events'].append(self.db.record_event(event))
            self.db.complete_command(command['id'])
            ctx['changes'].append(f'{entity["name"]}: {p["parameter"]} {old} → {p["value"]}')
        return len(commands)

    def _integrity(self,ctx):
        errors=self.db.check_integrity()
        if errors: raise ValueError('; '.join(errors))
        ctx['summary']=validate_world(self.db.snapshot(),self.db.tiles(),self.db.campaign(),require_connected=False)
        return ctx['summary']['cities']+ctx['summary']['provinces']

    def _snapshot(self,ctx):
        ctx['snapshot']=dict(self.db.counts(),population=ctx['summary']['population'])
        return len(ctx['snapshot'])

    def finish(self,command_id=None,source='player'):
        command_id=str(uuid4()) if command_id is None else command_id
        if not isinstance(command_id,str) or not command_id.strip(): raise ValueError('Требуется command_id')
        start=time.perf_counter(); ctx=dict(command_id=command_id,events=[],changes=[],warnings=[],phases=[])
        with self.db.transaction():
            existing=self.db.turn_result(command_id)
            if existing: return existing
            config=self.db.campaign()
            if not config: raise ValueError('Старая кампания: создайте новую с картой')
            config['status']='processing'; self.db.save_campaign(config)
            for name,handler in self.phases:
                phase_start=time.perf_counter(); count=handler(ctx)
                duration=time.perf_counter()-phase_start
                ctx['phases'].append(dict(name=name,processed=count,duration=duration))
                log.info('command_id=%s phase=%s processed=%s duration=%.4f',command_id,name,count,duration)
            old=self.db.meta()['turn']; self.db.set_turn(old+1)
            completed_month=config['month']; completed_year=config['year']
            config['month']=config['month']%12+1
            config['year']+=int(config['month']==1); config['status']='ready'
            self.db.save_campaign(config)
            report=dict(command_id=command_id,turn=old+1,completed_month=completed_month,completed_year=completed_year,
                        month=config['month'],year=config['year'],status='completed',phases=ctx['phases'],
                        changes=ctx['changes'],warnings=ctx['warnings'],snapshot=ctx['snapshot'],duration=time.perf_counter()-start,
                        summary='Месяц завершён, существенных изменений нет' if not ctx['changes'] else f'Месяц завершён: изменений {len(ctx["changes"])}')
            event=self.service.event('turn_advanced',dict(old=old,new=old+1,report=report),[],source=source,
                                     action_id=command_id,summary=report['summary'])
            ctx['events'].append(self.db.record_event(event)); self.db.save_turn_result(command_id,report)
            commit_started=time.perf_counter()
        commit_seconds=time.perf_counter()-commit_started
        notify_started=time.perf_counter()
        for event in ctx['events']: self.service.notify(event)
        wall=time.perf_counter()-start
        notifications=time.perf_counter()-notify_started
        self.last_timings=dict(wall=wall,commit=commit_seconds,notifications=notifications,
            orchestration=wall-sum(p['duration'] for p in ctx['phases'])-commit_seconds-notifications)
        return report
