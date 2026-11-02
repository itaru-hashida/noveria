import argparse
import sys
import shutil
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.db import WorldDB
from loader import load_world


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='data/world.lbug')
    parser.add_argument('--new', action='store_true', help='Только новый путь')
    parser.add_argument('--recreate',action='store_true',help='Пересоздать с резервной копией')
    parser.add_argument('--confirm',action='store_true')
    args = parser.parse_args()
    if args.new and Path(args.db).exists():
        parser.error('Путь уже существует; укажите новое имя сохранения.')
    if args.recreate:
        if not args.confirm: parser.error('Требуется --confirm; остановите Streamlit')
        target=Path(args.db).resolve()
        staging=target.with_name(target.name+'.new-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
        with WorldDB(staging) as db:
            load_world(db)
            from core.integrity import validate_world
            validate_world(db.snapshot(),db.tiles(),db.campaign())
        if target.exists():
            with WorldDB(target,read_only=True): pass
            backup=target.with_name(target.name+'.backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
            shutil.copy2(target,backup)
            print('Резервная копия:',backup)
        staging.replace(target)
        print('Новая кампания:',target)
        return
    with WorldDB(args.db) as db:
        print('Начальный мир создан' if load_world(db) else 'Сохранение оставлено без изменений')
        print(db.counts())


if __name__ == '__main__':
    main()
