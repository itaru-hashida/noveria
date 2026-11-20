"""Генератор карты: две модели мира за одним публичным контрактом.

Модель выбирается полем `generation.model`:

  * `legacy` (по умолчанию) — прежняя синусоидальная карта. Это значение по
    умолчанию, поэтому сохранения, эталонные дайджесты и воспроизводимость
    старых миров не сдвигаются ни на бит;
  * `geological` — «живая геологическая карта»: плиты → рельеф → эрозия →
    климат → биомы → месторождения. Включается явно.

Публичный контракт одинаков для обеих моделей: `generate`, `topology`,
`route`, `validate_geography` и словарь клетки с полями `id, x, y, terrain,
province_id, road, resources, passable, cost` — их читают `core/db.py`,
экономика и UI.

`terrain` и в новой модели остаётся пятизначным классом совместимости ради
слоёв карты (море/равнина/лес/горы/река). Непрерывный рельеф живёт в
дополнительных полях клетки, которых у `legacy` просто нет:

  * `elevation`   — 0..1, высота после эрозии;
  * `relief`      — lowland / plain / hills / plateau / mountains / peaks;
  * `slope`       — уклон по соседям;
  * `temperature` — 0..1 после широты, высоты и шума;
  * `moisture`    — 0..1 после ветра, океана и дождевой тени хребтов;
  * `fertility`   — 0..1, плодородие;
  * `biome`       — климатическая зона;
  * `plate_id`, `plate_type`, `boundary` — тектоника;
  * `movement_cost` (= `cost`), `road_cost`, `water`, `danger`.

Геологическая модель детерминирована: общий seed раскладывается на
производные seeds через `derive_seed`, поэтому смена ресурсов не сдвигает
горы и реки.
"""

import hashlib
import heapq
import logging
import math
import random

import networkx as nx
import numpy as np

log = logging.getLogger(__name__)

MODELS = ('legacy', 'geological')
DEFAULT_MODEL = 'legacy'

# Геометрия мира. Долю суши подбираем так, чтобы карта оставалась связной:
# провинции и дороги строятся по 4-связности, поэтому изолированных островов
# в первой версии нет — только один материк и внутренние водоёмы.
LAND_SHARES = (0.64, 0.70, 0.76, 0.82)
MIN_LAND_SHARE_OF_TILES = 0.66

# Квантили рельефа по высоте суши. Пороги долей, а не абсолютных высот:
# абсолютные зависят от seed и давали то 3%, то 20% гор на разных мирах.
RELIEF_QUANTILES = (0.20, 0.62, 0.86, 0.94, 0.99)

# Базовые стоимости прохода по классам совместимого `terrain`.
BASE_COST = {'water': 0.0, 'plain': 1.0, 'forest': 2.0, 'mountain': 5.0, 'river': 2.5}

# Порядок соседей фиксирован: от него зависит разрешение ничьих в рельефе.
NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
CROSS = ((1, 0), (0, 1))


def derive_seed(seed, purpose):
    """Производный seed этапа: hash(seed, purpose), независимый от порядка вызовов."""
    digest = hashlib.sha256(f'{seed}:{purpose}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


# --- рельеф -----------------------------------------------------------------

def topology(tiles):
    graph = nx.Graph()
    cells = {(t['x'], t['y']): t for t in tiles}
    for t in tiles:
        if t['passable']:
            graph.add_node(t['id'])
    for (x, y), t in cells.items():
        if not t['passable']:
            continue
        for xy in ((x + 1, y), (x, y + 1)):
            n = cells.get(xy)
            if n and n['passable']:
                graph.add_edge(t['id'], n['id'],
                               weight=(t['cost'] + n['cost']) / 2 * (.35 if t['road'] and n['road'] else 1))
    return graph


def route(tiles, start, end):
    graph = topology(tiles)
    try:
        path = nx.shortest_path(graph, start, end, weight='weight')
        return dict(path=path, cost=nx.path_weight(graph, path, 'weight'), reachable=True)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return dict(path=[], cost=None, reachable=False)


def _partition(graph, seeds, rng):
    owners = {}
    queue = []
    # Общая стоимость ребра гарантирует связность дерева каждого региона.
    edges = sorted(tuple(sorted((a, b))) for a, b in graph.edges)
    costs = {edge: 1 + rng.random() * 2 for edge in edges}
    for identity, node in seeds.items():
        heapq.heappush(queue, (0, identity, node))
    while queue:
        distance, identity, node = heapq.heappop(queue)
        if node in owners:
            continue
        owners[node] = identity
        for neighbor in sorted(graph[node]):
            if neighbor not in owners:
                heapq.heappush(queue, (distance + costs[tuple(sorted((node, neighbor)))], identity, neighbor))
    return owners


def _seeds(graph, identities, rng):
    nodes = sorted(graph)
    if len(nodes) < len(identities):
        raise ValueError('Недостаточно клеток для провинций')
    selected = [rng.choice(nodes)]
    while len(selected) < len(identities):
        distances = nx.multi_source_dijkstra_path_length(graph, selected)
        selected.append(max(nodes, key=lambda n: (distances[n], n)))
    return dict(zip(sorted(identities), selected))


# --- шум --------------------------------------------------------------------

def _lattice_noise(width, height, scale, rng):
    """Гладкий value-noise: масштаб задаётся в клетках."""
    scale = max(1.0, float(scale))
    grid_w = int(math.ceil(width / scale)) + 2
    grid_h = int(math.ceil(height / scale)) + 2
    values = [rng.random() for _ in range(grid_w * grid_h)]
    grid = np.array(values, dtype=np.float64).reshape(grid_h, grid_w)
    xs = np.arange(width, dtype=np.float64) / scale
    ys = np.arange(height, dtype=np.float64) / scale
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    tx = xs - x0
    ty = ys - y0
    sx = tx * tx * (3 - 2 * tx)
    sy = ty * ty * (3 - 2 * ty)
    a = grid[np.ix_(y0, x0)]
    b = grid[np.ix_(y0, x0 + 1)]
    c = grid[np.ix_(y0 + 1, x0)]
    d = grid[np.ix_(y0 + 1, x0 + 1)]
    top = a + (b - a) * sx[None, :]
    bottom = c + (d - c) * sx[None, :]
    return top + (bottom - top) * sy[:, None]


def _fbm(width, height, scale, octaves, rng, persistence=0.5):
    """Multi-octave noise; результат в 0..1."""
    total = np.zeros((height, width), dtype=np.float64)
    amplitude = 1.0
    norm = 0.0
    for _ in range(octaves):
        total += amplitude * _lattice_noise(width, height, scale, rng)
        norm += amplitude
        amplitude *= persistence
        scale = max(1.0, scale / 2.0)
    return total / norm


# --- плиты ------------------------------------------------------------------

def _plate_sites(width, height, count, rng):
    """Детерминированные центры плит: farthest-point sampling по сетке."""
    cells = [(x, y) for y in range(height) for x in range(width)]
    first = cells[rng.randrange(len(cells))]
    sites = [first]
    best = [(x - first[0]) ** 2 + (y - first[1]) ** 2 for x, y in cells]
    while len(sites) < count:
        index = max(range(len(cells)), key=lambda i: (best[i], -i))
        x, y = cells[index]
        sites.append((x, y))
        for i, (cx, cy) in enumerate(cells):
            distance = (cx - x) ** 2 + (cy - y) ** 2
            if distance < best[i]:
                best[i] = distance
    return sites


def _plate_voronoi(width, height, sites):
    """Геодезический Voronoi с 8-соседством: границы плит не «ступеньки»."""
    distance = np.full((height, width), np.inf, dtype=np.float64)
    owner = np.full((height, width), -1, dtype=np.int64)
    queue = []
    for index, (x, y) in enumerate(sites):
        distance[y, x] = 0.0
        owner[y, x] = index
        heapq.heappush(queue, (0.0, index, y, x))
    while queue:
        current, index, y, x = heapq.heappop(queue)
        if current > distance[y, x] + 1e-12:
            continue
        for dy, dx in NEIGHBOURS:
            ny, nx_ = y + dy, x + dx
            if not (0 <= ny < height and 0 <= nx_ < width):
                continue
            step = math.sqrt(dx * dx + dy * dy)
            candidate = current + step
            if candidate < distance[ny, nx_] - 1e-12:
                distance[ny, nx_] = candidate
                owner[ny, nx_] = index
                heapq.heappush(queue, (candidate, index, ny, nx_))
    return owner


def _tectonics(width, height, count, rng):
    sites = _plate_sites(width, height, count, rng)
    owner = _plate_voronoi(width, height, sites)
    plates = []
    for index, (x, y) in enumerate(sites):
        angle = rng.random() * 2 * math.pi
        speed = 0.35 + rng.random() * 0.9
        plates.append(dict(
            id=index, x=x, y=y,
            kind='continental' if rng.random() < 0.45 else 'oceanic',
            motion=(math.cos(angle) * speed, math.sin(angle) * speed),
            speed=speed,
            age=rng.random(),
        ))
    # Хотя бы одна континентальная и одна океаническая плита: иначе не будет
    # ни материка, ни океана, а вся карта выродится в один класс.
    plates[0]['kind'] = 'continental'
    if len(plates) > 1:
        plates[1]['kind'] = 'oceanic'
    return owner, plates


def _boundary_fields(width, height, owner, plates):
    """Поля влияния границ: орогенез, вулканизм, рифты, разломы."""
    seeds = {name: {} for name in ('convergent', 'arc', 'trench', 'rift', 'ridge', 'fault', 'volcano')}
    for y in range(height):
        for x in range(width):
            a = int(owner[y, x])
            for dy, dx in CROSS:
                ny, nx_ = y + dy, x + dx
                if ny >= height or nx_ >= width:
                    continue
                b = int(owner[ny, nx_])
                if a == b:
                    continue
                normal = (float(dx), float(dy))
                motion_a = plates[a]['motion']
                motion_b = plates[b]['motion']
                dv = (motion_a[0] - motion_b[0], motion_a[1] - motion_b[1])
                approach = -(dv[0] * normal[0] + dv[1] * normal[1])
                a_cont = plates[a]['kind'] == 'continental'
                b_cont = plates[b]['kind'] == 'continental'
                if approach > 0.18:
                    if a_cont and b_cont:
                        seeds['convergent'][(y, x)] = 0.95
                        seeds['convergent'][(ny, nx_)] = 0.95
                        seeds['volcano'][(y, x)] = max(seeds['volcano'].get((y, x), 0.0), 0.35)
                    elif a_cont or b_cont:
                        land_tile = (y, x) if a_cont else (ny, nx_)
                        ocean_tile = (ny, nx_) if a_cont else (y, x)
                        seeds['convergent'][land_tile] = 0.62
                        seeds['trench'][ocean_tile] = 0.55
                        seeds['volcano'][land_tile] = 0.72
                        seeds['arc'][land_tile] = 0.35
                    else:
                        # Океан-океан: подводная дуга, но островов не создаём —
                        # карта обязана оставаться связной.
                        seeds['ridge'][(y, x)] = max(seeds['ridge'].get((y, x), 0.0), 0.30)
                        seeds['ridge'][(ny, nx_)] = max(seeds['ridge'].get((ny, nx_), 0.0), 0.30)
                elif approach < -0.18:
                    strength = 0.65 if (a_cont and b_cont) else 0.35
                    seeds['rift'][(y, x)] = strength
                    seeds['rift'][(ny, nx_)] = strength
                    if not a_cont and not b_cont:
                        seeds['ridge'][(y, x)] = max(seeds['ridge'].get((y, x), 0.0), 0.25)
                        seeds['ridge'][(ny, nx_)] = max(seeds['ridge'].get((ny, nx_), 0.0), 0.25)
                else:
                    seeds['fault'][(y, x)] = max(seeds['fault'].get((y, x), 0.0), 0.55)
                    seeds['fault'][(ny, nx_)] = max(seeds['fault'].get((ny, nx_), 0.0), 0.55)
    # Затухание наружу от границы: широкие хребты, узкие разломы.
    return {
        'convergent': _spread((height, width), seeds['convergent'], 0.62, 5),
        'arc': _spread((height, width), seeds['arc'], 0.60, 6),
        'trench': _spread((height, width), seeds['trench'], 0.58, 3),
        'rift': _spread((height, width), seeds['rift'], 0.58, 4),
        'ridge': _spread((height, width), seeds['ridge'], 0.64, 5),
        'fault': _spread((height, width), seeds['fault'], 0.50, 2),
        'volcano': _spread((height, width), seeds['volcano'], 0.72, 2),
    }


def _spread(shape, seeds, decay, steps):
    """Распространение силы границы от её клеток с затуханием."""
    field = np.zeros(shape, dtype=np.float64)
    for (y, x), value in seeds.items():
        if value > field[y, x]:
            field[y, x] = value
    for _ in range(steps):
        padded = np.pad(field, 1, mode='constant', constant_values=0.0)
        best = np.zeros(shape, dtype=np.float64)
        for dy, dx in NEIGHBOURS:
            np.maximum(best, padded[1 + dy:1 + dy + shape[0], 1 + dx:1 + dx + shape[1]] * decay, out=best)
        field = np.maximum(field, best)
    return field


def _base_height(width, height, owner, plates, fields, rng):
    """Континентальная форма + границы плит + шум."""
    continental = np.array([p['kind'] == 'continental' for p in plates], dtype=np.float64)
    bias = continental[np.clip(owner, 0, None)] * 0.55 - 0.45
    # Древние континентальные щиты чуть выше: у них толстая кора.
    age = np.array([p['age'] for p in plates], dtype=np.float64)[np.clip(owner, 0, None)]
    bias = bias + (age - 0.5) * 0.10 * continental[np.clip(owner, 0, None)]

    warp = _fbm(width, height, width / 2.6, 5, rng)
    ridges = _fbm(width, height, max(2.0, width / 8.0), 4, rng)
    detail = _fbm(width, height, 2.4, 3, rng)

    height_field = bias
    height_field = height_field + 0.62 * (warp - 0.5)
    height_field = height_field + 0.26 * (ridges - 0.5)
    height_field = height_field + 0.07 * (detail - 0.5)
    height_field = height_field + 0.62 * fields['convergent']
    height_field = height_field + 0.30 * fields['arc']
    height_field = height_field + 0.26 * fields['volcano'] * (detail - 0.35)
    height_field = height_field + 0.10 * fields['ridge']
    height_field = height_field - 0.30 * fields['trench']
    height_field = height_field - 0.26 * fields['rift']
    height_field = height_field - 0.07 * fields['fault']
    return height_field


def _normalize(values):
    low = float(values.min())
    high = float(values.max())
    if high - low < 1e-9:
        return np.full(values.shape, 0.5, dtype=np.float64)
    return (values - low) / (high - low)


# --- гидрология -------------------------------------------------------------

def _priority_flood(height, sea_level):
    """Заполнение впадин: поверхность, по которой вода уже может течь."""
    rows, columns = height.shape
    filled = height.astype(np.float64).copy()
    closed = np.zeros((rows, columns), dtype=bool)
    queue = []

    def push(y, x):
        if closed[y, x]:
            return
        closed[y, x] = True
        level = max(float(height[y, x]), sea_level)
        filled[y, x] = level
        heapq.heappush(queue, (level, y, x))

    for x in range(columns):
        push(0, x)
        push(rows - 1, x)
    for y in range(rows):
        push(y, 0)
        push(y, columns - 1)
    while queue:
        level, y, x = heapq.heappop(queue)
        for dy, dx in NEIGHBOURS:
            ny, nx_ = y + dy, x + dx
            if not (0 <= ny < rows and 0 <= nx_ < columns) or closed[ny, nx_]:
                continue
            closed[ny, nx_] = True
            filled[ny, nx_] = max(float(height[ny, nx_]), level)
            heapq.heappush(queue, (float(filled[ny, nx_]), ny, nx_))
    return filled


def _flow_targets(filled, land):
    """Направление стока: самый низкий из четырёх соседей.

    Только 4-соседство: сетка `topology` соединяет клетки ортогонально, и
    диагональный сток давал бы реки, разорванные с точки зрения дорог и
    провинций. Ничьи разрешаются порядком CROSS.
    """
    rows, columns = filled.shape
    flat = filled.ravel()
    target = np.full(rows * columns, -1, dtype=np.int64)
    order = np.argsort(-flat, kind='stable')
    for index in order:
        if not land.flat[index]:
            continue
        y, x = divmod(int(index), columns)
        best = flat[index]
        chosen = -1
        for dy, dx in CROSS:
            for sign in (1, -1):
                ny, nx_ = y + dy * sign, x + dx * sign
                if not (0 <= ny < rows and 0 <= nx_ < columns):
                    continue
                candidate = flat[ny * columns + nx_]
                if candidate < best - 1e-12:
                    best = candidate
                    chosen = ny * columns + nx_
        target[index] = chosen
    return target


def _flow_accumulation(filled, target, land):
    """Водосбор: сколько клеток стекает через данную."""
    size = filled.size
    accumulation = np.ones(size, dtype=np.float64)
    order = np.argsort(-filled.ravel(), kind='stable')
    for index in order:
        receiver = target[index]
        if receiver >= 0 and land.flat[index]:
            accumulation[receiver] += accumulation[index]
    return accumulation


def _hydrology(height, sea_level, iterations, rng):
    """Эрозия водосборами: прорезаем долины и получаем реки."""
    current = height.astype(np.float64).copy()
    strength = 0.030
    for step in range(max(1, iterations)):
        filled = _priority_flood(current, sea_level)
        land = filled > sea_level
        target = _flow_targets(filled, land)
        accumulation = _flow_accumulation(filled, target, land)
        flat_current = current.ravel()
        flat_filled = filled.ravel()
        drop = np.zeros(flat_current.size, dtype=np.float64)
        valid = target >= 0
        drop[valid] = np.maximum(0.0, flat_filled[valid] - flat_filled[target[valid]])
        scaled = np.power(accumulation / max(1.0, float(accumulation.max())), 0.45)
        incision = strength * scaled * np.minimum(drop, 0.05) / 0.05
        incision[~land.ravel()] = 0.0
        current = (flat_current - incision).reshape(current.shape)
        # Каждый следующий проход слабее: долины уже прорезаны.
        strength *= 0.55
    filled = _priority_flood(current, sea_level)
    target = _flow_targets(filled, filled > sea_level)
    accumulation = _flow_accumulation(filled, target, filled > sea_level)
    return current, filled, target, accumulation


def _largest_component(mask):
    """4-связная компонента суши: `topology` соединяет только ортогональных соседей."""
    rows, columns = mask.shape
    component = np.zeros_like(mask, dtype=np.int64)
    best_label, best_size, label = 0, 0, 0
    for y in range(rows):
        for x in range(columns):
            if not mask[y, x] or component[y, x]:
                continue
            label += 1
            stack = [(y, x)]
            component[y, x] = label
            size = 0
            while stack:
                cy, cx = stack.pop()
                size += 1
                for ny, nx_ in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < rows and 0 <= nx_ < columns and mask[ny, nx_] and not component[ny, nx_]:
                        component[ny, nx_] = label
                        stack.append((ny, nx_))
            if size > best_size:
                best_label, best_size = label, size
    return component, best_label, best_size


def _choose_sea_level(height):
    """Уровень моря, при котором материк доминирует, а не распадается на клочки."""
    for share in LAND_SHARES:
        sea_level = float(np.quantile(height, 1.0 - share))
        land = height > sea_level
        if not land.any():
            continue
        _labels, _best, size = _largest_component(land)
        if size >= MIN_LAND_SHARE_OF_TILES * land.sum():
            return sea_level
    return float(np.quantile(height, 1.0 - LAND_SHARES[-1]))


# --- климат и биомы ---------------------------------------------------------

def _slope(elevation):
    padded = np.pad(elevation, 1, mode='edge')
    gradient = np.zeros(elevation.shape, dtype=np.float64)
    for dy, dx in NEIGHBOURS:
        shifted = padded[1 + dy:1 + dy + elevation.shape[0], 1 + dx:1 + dx + elevation.shape[1]]
        np.maximum(gradient, np.abs(elevation - shifted), out=gradient)
    return gradient


def _climate(elapsed_height, above, land, sea_level, width, height, rng):
    rows = np.arange(height, dtype=np.float64)[:, None]
    latitude = (rows + 0.5) / height
    warmth = 1.0 - np.abs(2.0 * latitude - 1.0)
    warmth = np.repeat(warmth, width, axis=1)
    noise = _fbm(width, height, max(3.0, width / 5.0), 3, rng)
    temperature = np.clip(warmth * 0.95 + 0.20 * (noise - 0.5) - 0.95 * above, 0.0, 1.0)

    ocean = ~land
    # Ветер по широтным полосам: пассаты, западные, полярные восточные.
    wind = np.zeros(height, dtype=np.int64)
    for y in range(height):
        band = abs((y + 0.5) / height - 0.5)
        if band < 0.17:
            wind[y] = -1
        elif band < 0.35:
            wind[y] = 1
        else:
            wind[y] = -1

    moisture = np.where(ocean, 1.0, 0.0)
    moisture_noise = _fbm(width, height, max(2.0, width / 6.0), 3, rng)
    for y in range(height):
        direction = int(wind[y])
        order = range(width - 1, -1, -1) if direction < 0 else range(width)
        for x in order:
            if ocean[y, x]:
                moisture[y, x] = 1.0
                continue
            upwind = x + 1 if direction < 0 else x - 1
            if 0 <= upwind < width:
                carried = moisture[y, upwind]
                rise = elapsed_height[y, x] - elapsed_height[y, upwind]
            else:
                carried = 0.55
                rise = 0.0
            value = 0.05 + 0.97 * carried - 1.7 * max(0.0, rise)
            moisture[y, x] = min(1.0, max(0.0, value))
    cooling = np.clip(warmth, 0.0, 1.0)
    moisture = np.clip(moisture * (0.55 + 0.45 * cooling) + 0.12 * (moisture_noise - 0.5), 0.0, 1.0)
    moisture[ocean] = 1.0
    return temperature, moisture, wind


def _biome(temperature, moisture, above, relief, land, river, water_kind):
    biome = np.full(temperature.shape, 'ocean', dtype=object)
    biome[water_kind == 'lake'] = 'lake'
    cold = temperature < 0.17
    cool = temperature < 0.32
    mild = temperature < 0.55
    warm = temperature < 0.75

    biome[land & cold] = 'tundra'
    biome[land & cool & (moisture > 0.35)] = 'taiga'
    biome[land & cool & (moisture <= 0.35)] = 'tundra'
    biome[land & mild & (moisture > 0.55)] = 'temperate_forest'
    biome[land & mild & (moisture > 0.34) & (moisture <= 0.55)] = 'woodland'
    biome[land & mild & (moisture <= 0.34)] = 'steppe'
    biome[land & warm & (moisture > 0.70)] = 'rainforest'
    biome[land & warm & (moisture > 0.48) & (moisture <= 0.70)] = 'temperate_forest'
    biome[land & warm & (moisture > 0.26) & (moisture <= 0.48)] = 'savanna'
    biome[land & warm & (moisture <= 0.26)] = 'desert'
    biome[land & ~warm & (moisture > 0.62)] = 'jungle'
    biome[land & ~warm & (moisture > 0.30) & (moisture <= 0.62)] = 'savanna'
    biome[land & ~warm & (moisture <= 0.30)] = 'desert'
    # Высокогорье определяется рельефом: плато тёплое, пики — всегда альпийские.
    alpine = land & np.isin(relief, ['mountains', 'peaks'])
    biome[alpine] = 'alpine'
    biome[land & (relief == 'peaks') & (temperature < 0.45)] = 'glacier'
    # Заболоченные низины: плохо и для дорог, и для земледелия без осушения.
    wetland = land & (relief == 'lowland') & (moisture > 0.78)
    biome[wetland] = 'wetland'
    biome[river & land] = 'river_valley'
    return biome


def _relief(above, slope, land):
    """Классы рельефа по квантилям высоты суши.

    Абсолютные пороги зависят от seed: на одном мире горы занимают 3% суши, на
    другом — 20%. Квантили дают устойчивое разнообразие и предсказуемую долю
    непроходимых пиков.
    """
    relief = np.full(above.shape, 'water', dtype=object)
    values = above[land]
    if values.size == 0:
        return relief
    lowland, hills, plateau, mountains, peaks = np.quantile(values, RELIEF_QUANTILES)
    band = np.full(above.shape, 'plain', dtype=object)
    band[above <= lowland] = 'lowland'
    band[above > hills] = 'hills'
    band[above > plateau] = 'plateau'
    band[above > mountains] = 'mountains'
    band[above > peaks] = 'peaks'
    relief[land] = band[land]
    # Крутизна важнее абсолютной высоты: обрыв в предгорьях — уже горы.
    # Пики не понижаем: это отдельный непроходимый класс. Плато остаётся
    # плато: иначе крутые склоны съедали бы весь средний диапазон.
    rugged = land & (slope > 0.09) & (above > plateau) & (relief != 'peaks')
    relief[rugged] = 'mountains'
    return relief


def _terrain(land, river, relief, biome):
    terrain = np.full(relief.shape, 'water', dtype=object)
    terrain[land] = 'plain'
    forest = np.isin(biome, ['taiga', 'temperate_forest', 'rainforest', 'jungle', 'woodland'])
    terrain[land & forest] = 'forest'
    mountains = np.isin(relief, ['mountains', 'peaks']) | np.isin(biome, ['alpine', 'glacier'])
    terrain[land & mountains] = 'mountain'
    terrain[land & river] = 'river'
    return terrain


# --- ресурсы ----------------------------------------------------------------

def _geology_resources(owner, plates, fields, land, terrain, biome, river, rng):
    """Геологический профиль плит и границ: руды, соли, топливо, стройматериалы."""
    rows, columns = land.shape
    deposits = {}

    def add(cell, *names):
        if not land.flat[cell]:
            return
        bucket = deposits.setdefault(int(cell), [])
        for name in names:
            if name not in bucket:
                bucket.append(name)

    volcanic = (fields['volcano'] > 0.30) | (fields['arc'] > 0.20)
    orogeny = fields['convergent'] > 0.30
    rifts = fields['rift'] > 0.25
    faults = fields['fault'] > 0.25
    shield = np.zeros((rows, columns), dtype=bool)
    young = np.zeros((rows, columns), dtype=bool)
    for index, plate in enumerate(plates):
        zone = (owner == index) & land
        if plate['kind'] != 'continental':
            continue
        if plate['age'] > 0.62:
            shield |= zone
        else:
            young |= zone

    mountain_zone = (terrain == 'mountain') & land
    plain_zone = (terrain == 'plain') & land
    forest_zone = (terrain == 'forest') & land
    dry = np.isin(biome, ['desert', 'steppe', 'savanna'])
    basin = plain_zone & (fields['rift'] > 0.12)

    def spread(mask, count, radius, probability, *goods):
        for cell in _cluster_cells(mask, count, radius, rng, probability):
            add(cell, *goods)

    # Горные породы есть на каждой горной клетке: это ждёт экономика.
    for index in np.flatnonzero(mountain_zone.ravel()):
        add(int(index), 'камень', 'железо')

    spread(volcanic & mountain_zone, 6, 2, 0.70, 'медь')
    spread(volcanic & mountain_zone, 4, 1, 0.60, 'сера')
    spread(volcanic & mountain_zone, 3, 1, 0.60, 'обсидиан')
    spread(shield & mountain_zone, 5, 1, 0.55, 'золото')
    spread(faults & mountain_zone, 4, 1, 0.50, 'серебро')
    spread(shield & mountain_zone, 5, 2, 0.70, 'железо')
    spread(young & mountain_zone & orogeny, 4, 2, 0.60, 'железо')
    spread(basin | (plain_zone & (fields['rift'] > 0.18)), 5, 2, 0.60, 'уголь')
    spread(basin & (fields['rift'] > 0.22), 4, 2, 0.55, 'нефть')
    spread(plain_zone & ~dry, 6, 1, 0.50, 'глина')
    spread(plain_zone & dry, 5, 1, 0.50, 'песок')
    spread(young & rifts & mountain_zone, 3, 1, 0.50, 'олово')
    spread(orogeny & mountain_zone & (fields['convergent'] > 0.5), 3, 1, 0.50, 'мрамор')
    spread(forest_zone & (fields['rift'] < 0.1), 4, 2, 0.45, 'лес')

    for cell in np.flatnonzero(river.ravel()):
        add(int(cell), 'рыба', 'песок')
        if rng.random() < 0.5:
            add(int(cell), 'глина')
    return deposits


def _cluster_cells(mask, count, radius, rng, probability):
    """Кластер месторождения: центры по геологической маске, окрестность — по вероятности."""
    rows, columns = mask.shape
    candidates = np.flatnonzero(mask.ravel())
    if candidates.size == 0 or count <= 0:
        return []
    centers = rng.sample(sorted(int(i) for i in candidates), k=min(count, int(candidates.size)))
    cells = set()
    for center in centers:
        cy, cx = divmod(center, columns)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if abs(dy) + abs(dx) > radius:
                    continue
                ny, nx_ = cy + dy, cx + dx
                if not (0 <= ny < rows and 0 <= nx_ < columns) or not mask[ny, nx_]:
                    continue
                if (dy or dx) and rng.random() > probability:
                    continue
                cells.add(ny * columns + nx_)
    return sorted(cells)


def _base_resources(terrain, biome):
    """Базовые ресурсы по классу поверхности: те же, что ждёт экономика."""
    resources = {}
    biome_flat = biome.ravel()
    for index, kind in enumerate(terrain.ravel()):
        if kind == 'mountain':
            resources[int(index)] = ['железо', 'камень']
        elif kind == 'forest':
            resources[int(index)] = ['лес']
        elif kind == 'river':
            resources[int(index)] = ['рыба']
        elif kind == 'plain':
            # Пустыня и ледники не пашут; остальная равнина — зерно.
            biome_kind = biome_flat[index]
            if biome_kind in ('desert', 'glacier', 'alpine', 'tundra'):
                resources[int(index)] = ['песок'] if biome_kind == 'desert' else []
            else:
                resources[int(index)] = ['зерно']
    return resources


# --- города и дороги --------------------------------------------------------

def _city_score(tile, cells, occupied, rng):
    x, y = tile['x'], tile['y']
    neighbours = [cells.get((x + dx, y + dy)) for dx, dy in
                  ((-1, 0), (1, 0), (0, -1), (0, 1))]
    rivers = sum(1 for n in neighbours if n and n['terrain'] == 'river')
    water = sum(1 for n in neighbours if n and not n['passable'])
    open_land = sum(1 for n in neighbours if n and n['passable'])
    spacing = min((abs(x - ox) + abs(y - oy) for ox, oy in occupied), default=99)
    score = 1.6 * tile['fertility'] - 3.2 * tile['slope'] - 0.08 * tile['cost']
    score += 0.12 * len(tile['resources'])
    score += 0.30 * min(2, rivers) + 0.22 * min(2, water)
    score += 0.03 * open_land
    score -= 0.9 * tile['danger']
    score -= 0.35 * max(0, 3 - spacing)
    return score + rng.random() * 1e-6


def _place_cities(tiles, content, graph, rng):
    by_id = {t['id']: t for t in tiles}
    cells = {(t['x'], t['y']): t for t in tiles}
    placements = {}
    occupied = set()
    for city in sorted((e for e in content['entities'] if e['kind'] == 'City'), key=lambda e: e['id']):
        pid = next(r['source'] for r in content['links'] if r['kind'] == 'includes' and r['target'] == city['id'])
        candidates = [t for t in tiles if t['province_id'] == pid and t['id'] not in occupied]
        usable = [t for t in candidates if t['passable'] and t['terrain'] not in ('water', 'mountain')]
        if not usable:
            usable = [t for t in candidates if t['passable'] and t['terrain'] != 'water']
        if not usable:
            raise ValueError(f'Нет суши для города {city["id"]} в провинции {pid}')
        coords = {(t['x'], t['y']) for t in tiles if t['id'] in occupied}
        tile = max(usable, key=lambda t: _city_score(t, cells, coords, rng))
        if tile['terrain'] == 'mountain':
            # Столица в горах — горный перевал: город остаётся, рельеф смягчается.
            tile['terrain'] = 'plain'
            tile['cost'] = BASE_COST['plain']
            tile['movement_cost'] = tile['cost']
            tile['relief'] = 'plain'
        occupied.add(tile['id'])
        placements[city['id']] = dict(tile_id=tile['id'], province_id=pid)
    return placements


def _connect_cities(tiles, placements):
    graph = topology(tiles)
    by_id = {t['id']: t for t in tiles}
    complete = nx.Graph()
    for city in placements:
        complete.add_node(city)
    ids = sorted(placements)
    for i, a in enumerate(ids):
        ta = by_id[placements[a]['tile_id']]
        for b in ids[i + 1:]:
            tb = by_id[placements[b]['tile_id']]
            complete.add_edge(a, b, weight=abs(ta['x'] - tb['x']) + abs(ta['y'] - tb['y']))
    for a, b in nx.minimum_spanning_tree(complete).edges:
        path = nx.shortest_path(graph, placements[a]['tile_id'], placements[b]['tile_id'], weight='weight')
        for node in path:
            by_id[node]['road'] = True


def _close_river_bed(tile):
    """Сделать клетку речной долиной, не ломая её высоту и климат."""
    tile['terrain'] = 'river'
    tile['water'] = False
    tile['passable'] = True
    tile['cost'] = BASE_COST['river']
    tile['movement_cost'] = tile['cost']
    tile['road_cost'] = round(tile['cost'] * 2.4 + tile['slope'] * 12.0, 2)
    tile['biome'] = 'river_valley'
    tile['fertility'] = max(tile['fertility'], 0.65)
    for good in ('рыба', 'песок'):
        if good not in tile['resources']:
            tile['resources'].append(good)
    tile['resources'].sort()


def _carve_bed(path):
    """Проложить русло так, чтобы высота монотонно падала к устью.

    Принудительный ручей идёт по кратчайшему пути и может подниматься выше
    истока. Тогда это не река, а канал в гору: высоты вдоль русла
    выравниваются по уклону от истока к морю.
    """
    if len(path) < 2:
        return
    start = path[0]['elevation']
    end = path[-1]['elevation']
    span = len(path) - 1
    for order, tile in enumerate(path):
        target = start + (end - start) * order / span
        # Русло всегда чуть ниже берегов: иначе вода не потечёт.
        if order:
            target = min(target, path[order - 1]['elevation'] - 1e-4)
        if target < tile['elevation']:
            tile['elevation'] = round(max(0.0, target), 4)
        _close_river_bed(tile)
    path[0]['elevation'] = round(min(path[0]['elevation'], start), 4)


def _province_resources(tiles, content, width, height):
    """Гарантия базовых условий: зерно, лес и доступ к воде у каждой провинции.

    Провинция без воды не может ни поставить город с рыбным промыслом, ни
    торговать по реке. Такой провинции прокладывается короткий ручей от
    ближайшего моря: цепочка клеток превращается в реку, а не одна клетка —
    иначе получался бы разорванный сток. Правка точечная и не трогает высоты.
    """
    provinces = sorted(e['id'] for e in content['entities'] if e['kind'] == 'Province')
    cells = {(t['x'], t['y']): t for t in tiles}
    by_province = {}
    for tile in tiles:
        if tile['province_id']:
            by_province.setdefault(tile['province_id'], []).append(tile)

    def has_water(tile):
        if tile['terrain'] == 'river':
            return True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = cells.get((tile['x'] + dx, tile['y'] + dy))
            if n and (n['water'] or n['terrain'] == 'river'):
                return True
        return False

    def outlet(tile):
        """Кратчайший путь по суше от клетки до моря (4-связность)."""
        best = None
        for radius in range(1, max(width, height) + 1):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    target = cells.get((tile['x'] + dx, tile['y'] + dy))
                    if not target or not target['water']:
                        continue
                    path = _land_path(cells, tile, target, width, height)
                    if path and (best is None or len(path) < len(best)):
                        best = path
            if best is not None:
                return best
        return None

    for pid in provinces:
        land = by_province.get(pid, [])
        if not land:
            continue
        if not any('зерно' in t['resources'] for t in land):
            pool = [t for t in land if t['terrain'] in ('plain', 'river')] or land
            tile = min(pool, key=lambda t: (-t['fertility'], t['slope'], t['id']))
            tile['resources'].append('зерно')
            tile['fertility'] = max(tile['fertility'], 0.72)
        if not any('лес' in t['resources'] for t in land):
            pool = [t for t in land if t['terrain'] in ('forest', 'plain', 'river')] or land
            tile = min(pool, key=lambda t: (-t['moisture'], t['slope'], t['id']))
            tile['resources'].append('лес')
            tile['moisture'] = max(tile['moisture'], 0.45)
            if tile['biome'] in ('desert', 'glacier', 'alpine'):
                tile['biome'] = 'temperate_forest'
        if any(has_water(t) for t in land):
            continue
        candidates = sorted(land, key=lambda t: (t['slope'], -t['fertility'], t['id']))
        for start in candidates:
            path = outlet(start)
            if not path:
                continue
            # Русло помечается целиком, даже за границей провинции: река,
            # оборванная на границе, не даёт ни устья, ни торгового пути.
            if path and path[-1]['water']:
                path = path[:-1]
            _carve_bed(path)
            break


def _land_path(cells, start, target, width, height):
    """Путь по ортогональным соседям от суши до воды; None, если не связан."""
    queue = [(start['x'], start['y'])]
    previous = {(start['x'], start['y']): None}
    while queue:
        x, y = queue.pop(0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < width and 0 <= ny < height) or (nx, ny) in previous:
                continue
            tile = cells[(nx, ny)]
            if tile is target:
                path = [tile]
                cursor = (x, y)
                while cursor is not None:
                    path.append(cells[cursor])
                    cursor = previous[cursor]
                path.reverse()
                return [step for step in path if step['passable']]
            if not tile['passable']:
                continue
            previous[(nx, ny)] = (x, y)
            queue.append((nx, ny))
    return None


# --- сборка карты -----------------------------------------------------------

def _trace_rivers(filled, target, land, water, accumulation, threshold, width, height):
    """Русло от истока до моря: каждая река обязана иметь устье.

    Исток — клетка с водосбором выше порога. Дальше идём по стоку, пока не
    упрёмся в воду: так каждая река 4-связна и заканчивается в море или озере,
    а не обрывается на склоне. Ветка без устья отбрасывается целиком — она не
    река, а впадина на склоне.
    """
    river = np.zeros((height, width), dtype=bool)
    flat_land = land.ravel()
    flat_water = water.ravel()
    flat_accumulation = accumulation.ravel()
    sources = np.flatnonzero(flat_land & (flat_accumulation >= threshold))
    # Сначала длинные реки: их русла задают основные долины.
    sources = sorted((int(i) for i in sources), key=lambda i: (-flat_accumulation[i], i))
    for source in sources:
        path = []
        cursor = source
        seen = set()
        reaches_water = False
        while cursor >= 0 and cursor not in seen:
            seen.add(cursor)
            if flat_water[cursor]:
                reaches_water = True
                break
            if not flat_land[cursor]:
                break
            path.append(cursor)
            nxt = int(target[cursor])
            if nxt < 0:
                break
            cursor = nxt
        if not reaches_water:
            continue
        for cell in path:
            river.flat[cell] = True
    return river


def _generate_legacy(content):
    """Прежняя карта: синусоидальные пояса, без плит и климата.

    Оставлена дословно как модель по умолчанию, чтобы сохранения и эталонные
    дайджесты не менялись. Новые слои сюда не добавляются.
    """
    config = content['generation']; rng = random.Random(config['seed'])
    w, h = config['width'], config['height']
    if not 20 <= w <= 1000 or not 20 <= h <= 1000:
        raise ValueError('Размер карты вне диапазона 20–1000')
    tiles = []
    for y in range(h):
        coast = 2 + int(1.5 + math.sin(y * .48))
        for x in range(w):
            sea = x < coast or x >= w - coast
            terrain = 'water' if sea else ('river' if x == w // 2 + int(2 * math.sin(y * .3)) else
                'mountain' if math.sin(x * .39) + math.cos(y * .5) > 1.25 else
                'forest' if math.sin(x * .27 - y * .4) > .35 else 'plain')
            cost = {'water': 0, 'plain': 1, 'river': 2, 'forest': 2, 'mountain': 5}[terrain]
            resources = [] if sea else [dict(plain='зерно', forest='лес', mountain='железо', river='рыба')[terrain]]
            # Горная порода — отдельное месторождение, а не вариант железной руды:
            # каменоломни и рудники могут существовать на одной горной клетке.
            if terrain == 'mountain':
                resources.append('камень')
            tiles.append(dict(id=f'tile-{x}-{y}', x=x, y=y, terrain=terrain, province_id='', road=False,
                              resources=resources, passable=not sea, cost=cost))
    graph = topology(tiles)
    states = [e['id'] for e in content['entities'] if e['kind'] == 'State']
    state_seeds = _seeds(graph, states, rng)
    state_cells = _partition(graph, state_seeds, rng)
    ownership = {r['target']: r['source'] for r in content['links'] if r['kind'] == 'owns'}
    provinces = [e for e in content['entities'] if e['kind'] == 'Province']
    province_cells = {}
    for state in sorted(states):
        sub = graph.subgraph([n for n in graph if state_cells[n] == state])
        ids = [p['id'] for p in provinces if ownership[p['id']] == state]
        province_cells.update(_partition(sub, _seeds(sub, ids, rng), rng))
    by_id = {t['id']: t for t in tiles}
    for identity, pid in province_cells.items():
        by_id[identity]['province_id'] = pid
    placements = {}; occupied = set()
    for city in sorted((e for e in content['entities'] if e['kind'] == 'City'), key=lambda e: e['id']):
        pid = next(r['source'] for r in content['links'] if r['kind'] == 'includes' and r['target'] == city['id'])
        candidates = [t for t in tiles if t['province_id'] == pid and t['id'] not in occupied]
        candidates.sort(key=lambda t: (t['terrain'] == 'mountain',
                                       -sum(by_id[n]['terrain'] == 'river' for n in graph[t['id']]),
                                       t['cost'], rng.random()))
        tile = candidates[0]
        if tile['terrain'] == 'mountain':
            tile['terrain'] = 'plain'; tile['cost'] = 1
        occupied.add(tile['id']); placements[city['id']] = dict(tile_id=tile['id'], province_id=pid)
    # Минимальное дерево соединяет все города, включая соседние государства.
    complete = nx.Graph()
    for city, p in placements.items():
        complete.add_node(city)
    ids = sorted(placements)
    for i, a in enumerate(ids):
        ta = by_id[placements[a]['tile_id']]
        for b in ids[i + 1:]:
            tb = by_id[placements[b]['tile_id']]
            complete.add_edge(a, b, weight=abs(ta['x'] - tb['x']) + abs(ta['y'] - tb['y']))
    for a, b in nx.minimum_spanning_tree(complete).edges:
        for node in nx.shortest_path(graph, placements[a]['tile_id'], placements[b]['tile_id'], weight='weight'):
            by_id[node]['road'] = True
    result = dict(tiles=tiles, placements=placements, config=config)
    validate_geography(result, content)
    return result


def generate(content):
    """Точка входа: выбрать модель мира из `generation.model`.

    По умолчанию `legacy`: старые миры и эталонные дайджесты остаются
    побитово прежними. Геологическая карта включается явно.
    """
    model = content['generation'].get('model', DEFAULT_MODEL)
    if model not in MODELS:
        raise ValueError(f'Неизвестная модель карты: {model}')
    if model == 'legacy':
        return _generate_legacy(content)
    return _generate_geological(content)


def _generate_geological(content):
    config = content['generation']
    width, height = int(config['width']), int(config['height'])
    if not 20 <= width <= 1000 or not 20 <= height <= 1000:
        raise ValueError('Размер карты вне диапазона 20–1000')
    seed = int(config['seed'])

    rng_plates = random.Random(derive_seed(seed, 'plates'))
    rng_height = random.Random(derive_seed(seed, 'height'))
    rng_erosion = random.Random(derive_seed(seed, 'erosion'))
    rng_climate = random.Random(derive_seed(seed, 'climate'))
    rng_resource = random.Random(derive_seed(seed, 'resources'))
    rng_regions = random.Random(derive_seed(seed, 'regions'))
    rng_cities = random.Random(derive_seed(seed, 'cities'))

    # 1. Тектоника: плиты, их движение и границы.
    area = width * height
    plate_count = max(8, min(30, round(area / 1600) + 6))
    owner, plates = _tectonics(width, height, plate_count, rng_plates)
    fields = _boundary_fields(width, height, owner, plates)

    # 2. Рельеф: континентальная форма + границы + multi-octave шум.
    raw_height = _base_height(width, height, owner, plates, fields, rng_height)
    raw_height = _normalize(raw_height)
    provisional_sea = float(np.quantile(raw_height, 0.38))

    # 3. Эрозия: вода идёт вниз, собирается в водосборы и прорезает долины.
    eroded, filled, target, accumulation = _hydrology(raw_height, provisional_sea, 3, rng_erosion)
    height_field = _normalize(eroded)
    sea_level = _choose_sea_level(height_field)
    filled = _priority_flood(height_field, sea_level)
    land = filled > sea_level
    target = _flow_targets(filled, land)
    accumulation = _flow_accumulation(filled, target, land).reshape(height_field.shape)

    # 4. Финальная вода: море, озёра и выбор связного материка.
    lake = land & (filled > height_field + 1e-6)
    passable = land & ~lake
    labels, main, size = _largest_component(passable)
    if main == 0 or size == 0:
        raise ValueError('Не удалось собрать материк')
    # Изолированные кусочки суши затапливаем: карта обязана быть связной.
    passable = passable & (labels == main)
    water = ~passable
    land = passable
    above = np.clip((height_field - sea_level) / max(1e-6, 1.0 - sea_level), 0.0, 1.0)
    above[water] = 0.0

    # 5. Гидрография: река начинается в водосборе и обязана дойти до моря.
    # Клетка с большим стоком без устья — это не река, а лужа на склоне,
    # поэтому строим русло по стоку до воды, а не берём порог по площади.
    threshold = max(6.0, 0.012 * float(land.sum()))
    river = _trace_rivers(filled, target, land, water, accumulation, threshold, width, height)

    # 6. Климат: широта, высота, ветер и дождевая тень хребтов.
    slope = _slope(height_field)
    temperature, moisture, _wind = _climate(height_field, above, land, sea_level,
                                             width, height, rng_climate)

    # 7. Биомы и рельефные классы.
    relief = _relief(above, slope, land)
    water_kind = np.where(water, np.where(lake, 'lake', 'ocean'), 'land')
    biome = _biome(temperature, moisture, above, relief, land, river, water_kind)
    terrain = _terrain(land, river, relief, biome)

    # 8. Проходимость: пики непроходимы, но только если материк остаётся
    # связным — иначе карта разваливается на куски, а провинции рвутся.
    peaks = (relief == 'peaks') & land
    if peaks.any():
        trial = land & ~peaks
        labels_trial, main_trial, size_trial = _largest_component(trial)
        if main_trial and size_trial == int(trial.sum()):
            land = trial
            passable = trial
            water = ~passable
            above[land] = np.clip((height_field[land] - sea_level) / max(1e-6, 1.0 - sea_level), 0.0, 1.0)
            terrain[~land] = 'water'
            biome[~land] = np.where(lake[~land], 'lake', 'ocean')
            river &= land

    # 9. Геология и ресурсы: кластеры по плитам и границам.
    deposits = _geology_resources(owner, plates, fields, land, terrain, biome, river, rng_resource)
    resources = _base_resources(terrain, biome)
    for cell, names in deposits.items():
        bucket = resources.setdefault(int(cell), [])
        for name in names:
            if name not in bucket:
                bucket.append(name)

    coastline = np.zeros((height, width), dtype=bool)
    for dy, dx in NEIGHBOURS:
        shifted = np.zeros((height, width), dtype=bool)
        ys = slice(max(0, dy), height + min(0, dy))
        xs = slice(max(0, dx), width + min(0, dx))
        ys_source = slice(max(0, -dy), height + min(0, -dy))
        xs_source = slice(max(0, -dx), width + min(0, -dx))
        shifted[ys, xs] = water[ys_source, xs_source]
        coastline |= shifted
    coast = land & coastline

    fertility = np.clip(0.55 * moisture + 0.35 * (1.0 - above) - 0.55 * slope, 0.0, 1.0)
    fertility = np.where(np.isin(biome, ['desert', 'glacier', 'alpine']), fertility * 0.15, fertility)
    fertility[coast] = np.maximum(fertility[coast], 0.45)
    fertility[river] = np.maximum(fertility[river], 0.65)
    fertility[terrain == 'forest'] = np.maximum(fertility[terrain == 'forest'], 0.55)

    # 9. Сборка клеток: поля совместимости + непрерывные слои.
    tiles = []
    for y in range(height):
        for x in range(width):
            index = y * width + x
            kind = str(terrain[y, x])
            passable_cell = bool(passable[y, x])
            biome_kind = str(biome[y, x])
            relief_kind = str(relief[y, x])
            cost = BASE_COST[kind]
            if passable_cell:
                if biome_kind in ('wetland',):
                    cost += 1.5
                elif biome_kind in ('desert',):
                    cost += 0.5
                if relief_kind == 'hills':
                    cost += 0.5
            danger = 0.0
            if relief_kind in ('mountains', 'peaks'):
                danger += 0.45
            if biome_kind == 'wetland':
                danger += 0.35
            if biome_kind == 'desert':
                danger += 0.25
            danger += min(0.4, float(slope[y, x]) * 6.0)
            plate_index = int(owner[y, x])
            boundary = 'none'
            strength = 0.0
            for name, field in (('convergent', fields['convergent']), ('divergent', fields['rift']),
                                ('transform', fields['fault']), ('volcanic', fields['volcano'])):
                value = float(field[y, x])
                if value > strength:
                    boundary, strength = name, value
            tiles.append(dict(
                id=f'tile-{x}-{y}', x=x, y=y,
                terrain=kind,
                province_id='',
                road=False,
                resources=sorted(resources.get(index, [])),
                passable=passable_cell,
                cost=round(float(cost), 2),
                elevation=round(float(height_field[y, x]), 4),
                relief=relief_kind,
                slope=round(float(slope[y, x]), 4),
                temperature=round(float(temperature[y, x]), 4),
                moisture=round(float(moisture[y, x]), 4),
                fertility=round(float(fertility[y, x]), 4),
                biome=biome_kind,
                plate_id=plate_index,
                plate_type=str(plates[plate_index]['kind']),
                boundary=boundary if strength > 0.08 else 'none',
                movement_cost=round(float(cost), 2),
                road_cost=round(float(cost) * 2.4 + float(slope[y, x]) * 12.0, 2),
                water=not passable_cell,
                danger=round(float(danger), 3),
            ))

    graph = topology(tiles)
    states = [e['id'] for e in content['entities'] if e['kind'] == 'State']
    state_seeds = _seeds(graph, states, rng_regions)
    state_cells = _partition(graph, state_seeds, rng_regions)
    ownership = {r['target']: r['source'] for r in content['links'] if r['kind'] == 'owns'}
    provinces = [e for e in content['entities'] if e['kind'] == 'Province']
    province_cells = {}
    for state in sorted(states):
        sub = graph.subgraph([n for n in graph if state_cells[n] == state])
        ids = [p['id'] for p in provinces if ownership[p['id']] == state]
        province_cells.update(_partition(sub, _seeds(sub, ids, rng_regions), rng_regions))
    by_id = {t['id']: t for t in tiles}
    for identity, pid in province_cells.items():
        by_id[identity]['province_id'] = pid

    _province_resources(tiles, content, width, height)
    placements = _place_cities(tiles, content, graph, rng_cities)
    # Дороги детерминированы: MST по городам, путь — по стоимости прохода.
    _connect_cities(tiles, placements)

    result = dict(tiles=tiles, placements=placements, config=config)
    log.info('Карта %sx%s: плит=%s, суши=%s, рек=%s, биомов=%s',
             width, height, len(plates), int(land.sum()), int(river.sum()),
             len({t['biome'] for t in tiles}))
    validate_geography(result, content)
    return result


def validate_geography(result, content, require_connected=True):
    tiles = result['tiles']
    graph = topology(tiles)
    coords = {(t['x'], t['y']) for t in tiles}
    if len(coords) != len(tiles):
        raise ValueError('Повтор координат карты')
    by_id = {t['id']: t for t in tiles}
    provinces = {e['id']: e for e in content['entities'] if e['kind'] == 'Province'}
    owners = {r['target']: r['source'] for r in content['links'] if r['kind'] == 'owns'}
    for t in tiles:
        if t['passable'] and t['province_id'] not in provinces:
            raise ValueError('Суша без провинции')
        if t['road'] and not t['passable']:
            raise ValueError('Дорога на воде')
    for pid, p in provinces.items():
        nodes = [t['id'] for t in tiles if t['province_id'] == pid]
        if not nodes or not nx.is_connected(graph.subgraph(nodes)):
            raise ValueError(f'Разорвана провинция {pid}')
        capital = result['placements'].get(p['properties'].get('capital_id'))
        if not capital or capital['province_id'] != pid:
            raise ValueError('Столица вне провинции')
    for state in {owners[p] for p in provinces}:
        nodes = [t['id'] for t in tiles if owners.get(t['province_id']) == state]
        if require_connected and nodes and not nx.is_connected(graph.subgraph(nodes)):
            raise ValueError(f'Разорвана территория {state}')
    for city, p in result['placements'].items():
        t = by_id.get(p['tile_id'])
        if not t or t['province_id'] != p['province_id'] or t['terrain'] in ('water', 'mountain'):
            raise ValueError(f'Некорректное размещение города {city}')
