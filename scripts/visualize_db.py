import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.db import WorldDB, ENTITY_TYPES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='data/world.lbug')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if not Path(args.db).exists():
        parser.error('Сохранение не существует')
    with WorldDB(args.db, read_only=True) as db:
        print(json.dumps(dict(meta=db.meta(), counts=db.counts(), relation_counts=db.relation_counts(),
                              nodes={k:db.list_entities(k,limit=args.limit) for k in ENTITY_TYPES},
                              links=db.links(limit=args.limit), history=db.history(limit=args.limit)),
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
