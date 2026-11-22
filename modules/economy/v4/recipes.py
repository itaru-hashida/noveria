"""Числовая модель графа производственных рецептов V4.

Модуль намеренно не импортирует V3. Входом служит обычное отображение
конфигурации товаров, а состояние графа хранится только в NumPy-массивах и
неизменяемых кортежах идентификаторов.
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import numpy as np


class RecipeCycleError(ValueError):
    """Рецепты содержат цикл, поэтому валовой выпуск DAG не определён."""


def _as_nonnegative_float(value: Any, *, context: str) -> float:
    """Преобразовать число и дать понятную ошибку для плохих коэффициентов."""

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: требуется числовое значение") from exc
    if not np.isfinite(result) or result < 0:
        raise ValueError(f"{context}: значение должно быть конечным и неотрицательным")
    return result


def _recipe_inputs(goods: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Извлечь ``inputs`` из формата ``economy.json`` или простого mapping."""

    result: dict[str, Mapping[str, Any]] = {}
    for raw_good, raw_recipe in goods.items():
        good = str(raw_good)
        if not isinstance(raw_recipe, Mapping):
            raise ValueError(f"Рецепт товара {good!r} должен быть mapping")
        inputs = raw_recipe.get("inputs", raw_recipe)
        if not isinstance(inputs, Mapping):
            raise ValueError(f"Рецепт товара {good!r}: inputs должен быть mapping")
        result[good] = inputs
    return result


@dataclass(frozen=True)
class RecipeGraph:
    """Стабильный числовой контракт рецептов.

    ``A[input_index, output_index]`` — расход входного товара на единицу
    выпуска. Идентификаторы и топологический порядок отсортированы явно, так
    что порядок исходного JSON или Python-словаря не влияет на результат.
    """

    good_ids: tuple[str, ...]
    good_index: Mapping[str, int]
    A: np.ndarray
    topological_order: tuple[str, ...]
    levels: tuple[tuple[str, ...], ...]
    _topological_indices: tuple[int, ...]
    _level_indices: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        # Dataclass frozen защищает ссылку, а read-only защищает сами числа.
        self.A.setflags(write=False)

    @property
    def n_goods(self) -> int:
        return len(self.good_ids)

    @property
    def topological_indices(self) -> tuple[int, ...]:
        """Индексы от сырья к продуктам для пакетных вычислений."""

        return self._topological_indices

    def vector(self, demand: Mapping[str, float] | Any) -> np.ndarray:
        """Нормализовать конечный спрос в вектор порядка ``good_ids``."""

        if isinstance(demand, Mapping):
            vector = np.zeros(self.n_goods, dtype=np.float64)
            for raw_good, value in demand.items():
                good = str(raw_good)
                if good not in self.good_index:
                    raise KeyError(f"Неизвестный товар в спросе: {good}")
                vector[self.good_index[good]] = _as_nonnegative_float(
                    value, context=f"Спрос {good}"
                )
            return vector

        array = np.asarray(demand, dtype=np.float64)
        if array.ndim not in (1, 2) or array.shape[0] != self.n_goods:
            raise ValueError(
                "Спрос должен иметь форму (goods,) или (goods, scenarios)"
            )
        if not np.all(np.isfinite(array)) or np.any(array < 0):
            raise ValueError("Спрос должен быть конечным и неотрицательным")
        return array.copy()

    def theoretical_demand(self, final_demand: Mapping[str, float] | Any) -> np.ndarray:
        """Рассчитать ``q = f + A @ q`` обратным проходом по уровням DAG.

        Для матрицы сценариев второй размер обрабатывается broadcasting-ом и
        одним и тем же проходом по уровням. Это теоретическая потребность, а
        не обещание, что такой объём будет произведён или куплен.
        """

        result = self.vector(final_demand)
        was_vector = result.ndim == 1
        if was_vector:
            result = result[:, np.newaxis]

        for level in reversed(self._level_indices):
            if not level:
                continue
            # В DAG входы уровня находятся строго раньше него. Поэтому после
            # добавления вклада текущего уровня его значение больше не меняется
            # поздними уровнями.
            result += self.A[:, level] @ result[list(level), :]

        if was_vector:
            return result[:, 0]
        return result

    # Явное короткое имя удобно в экспериментальных ноутбуках и тестах.
    gross_requirements = theoretical_demand


def build_recipe_graph(goods: Mapping[str, Any]) -> RecipeGraph:
    """Построить граф из ``good_id -> {inputs: {input_id: ratio}}``."""

    inputs_by_good = _recipe_inputs(goods)
    good_ids = tuple(sorted(inputs_by_good))
    good_index = {good: index for index, good in enumerate(good_ids)}
    n_goods = len(good_ids)
    matrix = np.zeros((n_goods, n_goods), dtype=np.float64)
    children: list[set[int]] = [set() for _ in range(n_goods)]
    indegree = np.zeros(n_goods, dtype=np.int64)

    for output_good in good_ids:
        output_index = good_index[output_good]
        for raw_input_good, raw_ratio in sorted(
            inputs_by_good[output_good].items(), key=lambda item: str(item[0])
        ):
            input_good = str(raw_input_good)
            if input_good not in good_index:
                raise KeyError(
                    f"Рецепт {output_good!r} ссылается на неизвестный товар {input_good!r}"
                )
            ratio = _as_nonnegative_float(
                raw_ratio, context=f"Рецепт {output_good}/{input_good}"
            )
            if ratio == 0:
                continue
            input_index = good_index[input_good]
            matrix[input_index, output_index] = ratio
            children[input_index].add(output_index)
            indegree[output_index] += 1

    # Куча по строковому ID делает порядок стабильным даже при перестановке
    # исходных товаров и при наличии нескольких независимых ветвей.
    ready = [index for index, degree in enumerate(indegree) if degree == 0]
    heapq.heapify(ready)
    ordered_indices: list[int] = []
    while ready:
        current = heapq.heappop(ready)
        ordered_indices.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)

    if len(ordered_indices) != n_goods:
        cycle = _find_cycle(good_ids, children, indegree)
        path = " -> ".join(cycle)
        raise RecipeCycleError(f"Обнаружен цикл рецептов: {path}")

    levels_by_index = np.zeros(n_goods, dtype=np.int64)
    for current in ordered_indices:
        for child in children[current]:
            levels_by_index[child] = max(levels_by_index[child], levels_by_index[current] + 1)

    max_level = int(levels_by_index.max()) if n_goods else -1
    level_indices = tuple(
        tuple(index for index in ordered_indices if levels_by_index[index] == level)
        for level in range(max_level + 1)
    )
    levels = tuple(tuple(good_ids[index] for index in level) for level in level_indices)
    matrix.setflags(write=False)
    return RecipeGraph(
        good_ids=good_ids,
        good_index=MappingProxyType(good_index),
        A=matrix,
        topological_order=tuple(good_ids[index] for index in ordered_indices),
        levels=levels,
        _topological_indices=tuple(ordered_indices),
        _level_indices=level_indices,
    )


def _find_cycle(
    good_ids: tuple[str, ...],
    children: list[set[int]],
    remaining_indegree: np.ndarray,
) -> tuple[str, ...]:
    """Найти один стабильный цикл для диагностического сообщения."""

    remaining = {index for index, degree in enumerate(remaining_indegree) if degree > 0}
    state = np.zeros(len(good_ids), dtype=np.int8)
    path: list[int] = []

    def visit(current: int) -> tuple[str, ...] | None:
        state[current] = 1
        path.append(current)
        for child in sorted(children[current]):
            if child not in remaining:
                continue
            if state[child] == 0:
                found = visit(child)
                if found is not None:
                    return found
            elif state[child] == 1:
                start = path.index(child)
                return tuple(good_ids[index] for index in path[start:] + [child])
        path.pop()
        state[current] = 2
        return None

    for current in sorted(remaining):
        if state[current] == 0:
            found = visit(current)
            if found is not None:
                return found
    return ("<неизвестный цикл>",)


def load_economy_config(path: str | Path | None = None) -> dict[str, Any]:
    """Загрузить JSON-конфигурацию, не меняя исходный файл."""

    if path is None:
        path = Path(__file__).resolve().parents[3] / "content" / "economy.json"
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def load_recipe_graph(path: str | Path | None = None) -> RecipeGraph:
    """Построить граф из реального ``content/economy.json`` или его копии."""

    config = load_economy_config(path)
    goods = config.get("goods")
    if not isinstance(goods, Mapping):
        raise ValueError("В конфигурации отсутствует mapping goods")
    return build_recipe_graph(goods)


__all__ = [
    "RecipeCycleError",
    "RecipeGraph",
    "build_recipe_graph",
    "load_economy_config",
    "load_recipe_graph",
]
