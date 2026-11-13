"""Exact replay project_requests на границе каждого реального месяца."""
import argparse
import copy
import hashlib
import sys
import os
import subprocess
import types
from numbers import Real
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'audit')]
import scenario_runner as sr
from audit.profile_run import workdir
from modules.economy import Economy
from modules.economy.native import serial_step
from modules.economy.native.update_prices_boundary import run_native_project_requests


def rng_digest(rng):
    return hashlib.sha256(repr(rng.getstate()).encode()).hexdigest()


def exact(left, right, path='state'):
    # Integer/float представления равны только при точном числовом равенстве.
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return path + '.keys', list(left), list(right)
        for key in left:
            result = exact(left[key], right[key], path + '[' + repr(key) + ']')
            if result:
                return result
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return path + '.length', len(left), len(right)
        for i, (a, b) in enumerate(zip(left, right)):
            result = exact(a, b, path + '[' + str(i) + ']')
            if result:
                return result
    elif isinstance(left, bool) or isinstance(right, bool):
        if type(left) is not type(right) or left != right:
            return path, left, right
    elif isinstance(left, Real) and isinstance(right, Real):
        if left != right:
            return path, left, right
    elif left != right:
        return path, left, right
    return None


def reservation_snapshot(state):
    if state is None:
        return None
    return dict(flow=dict(state['flow_reserved']), inputs=dict(state['input_reserved']),
        labor=dict(state['labor_reserved']), resources=dict(state['resource_reserved']),
        supply_revision=dict(state['supply_revision']), dirty_goods=set(state['dirty_goods']),
        projects=copy.deepcopy(state['index']['projects']),
        project_revision=state['index']['project_revision'],
        flow_balances=copy.deepcopy(state['flow_balances']))


def compare(seed, months):
    original = Economy.project_requests
    calls = 0
    native_entry = serial_step.native_project_requests

    def counted(*args):
        nonlocal calls
        calls += 1
        return native_entry(*args)

    def replay(self, sells):
        native, native_sells = copy.deepcopy((self, sells))
        native._native_project_trace = True
        before = rng_digest(self.rng)
        trace = []
        original_candidate = Economy.project_candidate
        original_register = Economy.register_investment_project
        original_limits = Economy.investment_candidate_capacity_limits
        final_reservations = None

        def traced_candidate(engine, investor, *args, **kwargs):
            rng_before = rng_digest(engine.rng)
            project = original_candidate(engine, investor, *args, **kwargs)
            trace.append(dict(execution_seq=len(trace), event='candidate',
                investor=investor['id'], project=copy.deepcopy(project),
                rng_before=rng_before, rng_after=rng_digest(engine.rng)))
            return project

        def traced_register(engine, project):
            original_register(engine, project)
            state = getattr(engine, '_investment_state', None)
            reservations = None if state is None else dict(
                flow=dict(state['flow_reserved']), inputs=dict(state['input_reserved']),
                labor=dict(state['labor_reserved']), resources=dict(state['resource_reserved']),
                supply_revision=dict(state['supply_revision']), dirty_goods=set(state['dirty_goods']),
                projects=copy.deepcopy(state['index']['projects']),
                project_revision=state['index']['project_revision'])
            trace.append(dict(execution_seq=len(trace), event='register_project',
                project=copy.deepcopy(project), reservations=reservations))

        def traced_limits(engine, *args, **kwargs):
            nonlocal final_reservations
            result = original_limits(engine, *args, **kwargs)
            final_reservations = reservation_snapshot(engine._investment_state)
            return result

        Economy.investment_candidate_capacity_limits = traced_limits
        Economy.project_candidate = traced_candidate
        Economy.register_investment_project = traced_register
        try:
            original(self, sells)
        finally:
            Economy.project_candidate = original_candidate
            Economy.register_investment_project = original_register
            Economy.investment_candidate_capacity_limits = original_limits
        old_calls = calls
        run_native_project_requests(native, native_sells)
        assert calls - old_calls == 1, 'Должен быть ровно один Cython entry call'
        journal = native.native_project_requests['journal']
        for seq, (left, right) in enumerate(zip(trace, journal)):
            result = exact(left, right, 'journal')
            if result:
                field, v3_value, native_value = result
                raise AssertionError(dict(seed=seed, month=self.meta['month'],
                    investor=left.get('investor'), execution_seq=seq, event=left['event'],
                    field=field, V3=v3_value, native=native_value,
                    rng_before=left.get('rng_before'), rng_after_v3=left.get('rng_after'),
                    rng_after_native=right.get('rng_after')))
        assert len(trace) == len(journal), (len(trace), len(journal))
        result = exact(final_reservations,
            reservation_snapshot(native.native_project_requests['reservations']), 'final_reservations')
        assert result is None, dict(seed=seed, month=self.meta['month'],
            event='final_reservations', difference=result, rng_before=before,
            rng_after_v3=rng_digest(self.rng), rng_after_native=rng_digest(native.rng))
        for name in ('accounts', 'enterprises', 'warehouses', 'meta', 'report'):
            result = exact(getattr(self, name, None), getattr(native, name, None), name)
            if result:
                field, left, right = result
                raise AssertionError(dict(seed=seed, month=self.meta['month'],
                    investor=None, execution_seq=None, event='phase_result', field=field,
                    V3=left, native=right, rng_before=before,
                    rng_after_v3=rng_digest(self.rng), rng_after_native=rng_digest(native.rng)))
        assert self.rng.getstate() == native.rng.getstate()
        result = exact(sells, native_sells, 'offers')
        assert result is None, result
        self.validate()
        native.validate()
        print(f'seed={seed} month={self.meta["month"]}: exact, calls=1, events={len(trace)}, RNG=equal', flush=True)

    Economy.project_requests = replay
    serial_step.native_project_requests = counted
    try:
        with workdir(f'project_batch_{seed}_{os.getpid()}') as directory:
            db, service = sr.build_world(seed, directory, report_detail='deep')
            try:
                for month in range(months):
                    service.advance_turn(command_id=f'batch-{seed}-{month}', source='audit')
            finally:
                db.close()
    finally:
        Economy.project_requests = original
        serial_step.native_project_requests = native_entry


def compare_full_steps(seed, months, against_step5=False):
    from modules.economy.native import update_prices_boundary as boundary
    current_requests = boundary.run_native_project_requests
    if against_step5:
        source = subprocess.run(['git', 'show',
            '50ad666:modules/economy/native/update_prices_boundary.py'],
            cwd=ROOT, check=True, capture_output=True, text=True).stdout
        baseline = types.ModuleType('modules.economy.native._step5_boundary')
        exec(compile(source, '50ad666/update_prices_boundary.py', 'exec'), baseline.__dict__)
        baseline_requests = baseline.run_native_project_requests
    with workdir(f'project_fullstep_{seed}_{os.getpid()}') as directory:
        db, service = sr.build_world(seed, directory, report_detail='deep')
        try:
            reference = service.economy_state().engine
            native = copy.deepcopy(reference)
            for month in range(months):
                before = rng_digest(reference.rng)
                if against_step5:
                    boundary.run_native_project_requests = baseline_requests
                try:
                    reference.step(native_mode='cython' if against_step5 else 'off')
                finally:
                    boundary.run_native_project_requests = current_requests
                native.step(native_mode='cython')
                for name in ('accounts', 'enterprises', 'warehouses', 'meta', 'report'):
                    result = exact(getattr(reference, name, None), getattr(native, name, None), name)
                    if result:
                        field, left, right = result
                        raise AssertionError(dict(seed=seed, month=month+1, event='full_step',
                            field=field, V3=left, native=right, rng_before=before,
                            rng_after_v3=rng_digest(reference.rng), rng_after_native=rng_digest(native.rng)))
                assert reference.rng.getstate() == native.rng.getstate()
                reference.validate()
                native.validate()
                print(f'seed={seed} month={month+1}: full step exact, RNG=equal', flush=True)
        finally:
            db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--months', type=int, default=1)
    parser.add_argument('--full-step', action='store_true')
    parser.add_argument('--against-step5', action='store_true')
    args = parser.parse_args()
    if args.full_step or args.against_step5:
        compare_full_steps(args.seed, args.months, args.against_step5)
    else:
        compare(args.seed, args.months)
