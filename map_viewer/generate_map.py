import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from modules.geography.logic import generate


content = json.loads((root / 'content/initial_world.json').read_text(encoding='utf-8'))
WIDTH = HEIGHT = 1000
content['generation'].update(width=WIDTH, height=HEIGHT, seed=73129, model='geological')
world = generate(content)
payload = dict(
    width=WIDTH,
    height=HEIGHT,
    seed=73129,
    model='geological',
    tiles=world['tiles'],
    placements=world['placements'],
    entities=content['entities'],
    links=content['links'],
)
out = Path(__file__).resolve().parent / 'berta.json'
out.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
print(f'{out}: {len(world["tiles"])} tiles')
