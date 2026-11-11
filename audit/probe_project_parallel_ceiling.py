"""Measure the optimistic parallel ceiling of investment candidate evaluation."""
from collections import defaultdict
import argparse
from pathlib import Path
from time import perf_counter
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audit import scenario_runner as sr  # noqa: E402
from audit.profile_run import workdir  # noqa: E402
from modules.economy import Economy  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=73129)
    parser.add_argument('--months', type=int, default=12)
    args = parser.parse_args()
    names = ('project_requests', 'project_candidate', 'investment_demand',
             'upstream_expansion_candidate', 'build_investment_state',
             'profile', 'register_investment_project')
    original = {name: getattr(Economy, name) for name in names}
    seconds = defaultdict(float)
    calls = defaultdict(int)
    active = False
    measured = False

    for name, fn in original.items():
        def wrap(method, label):
            def timed(self, *args, **kwargs):
                nonlocal active
                scope = measured and (label == 'project_requests' or active)
                if label == 'project_requests':
                    active = True
                start = perf_counter() if scope else None
                try:
                    return method(self, *args, **kwargs)
                finally:
                    if start is not None:
                        seconds[label] += perf_counter()-start
                        calls[label] += 1
                    if label == 'project_requests':
                        active = False
            return timed
        setattr(Economy, name, wrap(fn, name))

    try:
        with workdir('project_parallel_ceiling') as tmp:
            db, service = sr.build_world(args.seed, tmp, report_detail='none')
            try:
                engine = service.economy_state().engine
                for _ in range(2):
                    engine.step()
                measured = True
                for _ in range(args.months):
                    engine.step()
                for name in names:
                    print(f'{name}: calls={calls[name]} seconds={seconds[name]:.4f}')
                total = seconds['project_requests']
                candidate = seconds['project_candidate']
                print(f'optimistic_candidate_share={candidate/max(total, 1e-9):.1%}')
            finally:
                db.close()
    finally:
        for name, fn in original.items():
            setattr(Economy, name, fn)


if __name__ == '__main__':
    main()
