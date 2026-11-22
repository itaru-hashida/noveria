"""Пакетный расчёт заявок производственных предприятий V4.

Здесь нет ссылок на ``Economy``, ``EconomyState`` или объекты V3. Строки
предприятий преобразуются один раз в числовые таблицы ``enterprise x good``;
дальнейший расчёт использует broadcasting и операции NumPy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .recipes import RecipeGraph


_EPS = 1e-12


def _number(value: Any, *, context: str, allow_infinity: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: требуется числовое значение") from exc
    if not np.isfinite(result) and not (allow_infinity and np.isinf(result)):
        raise ValueError(f"{context}: значение должно быть конечным")
    if result < 0:
        raise ValueError(f"{context}: значение должно быть неотрицательным")
    return result


def _mapping_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _normalise_id(value: Any, *, context: str) -> str:
    if value is None:
        raise ValueError(f"{context}: отсутствует id")
    result = str(value)
    if not result:
        raise ValueError(f"{context}: пустой id")
    return result


def _normalise_plan(row: Mapping[str, Any], graph: RecipeGraph) -> dict[str, float]:
    raw_plan = _mapping_value(row, "production_plan", "plan", "planned_output")
    if raw_plan is None:
        return {}
    if isinstance(raw_plan, Mapping):
        plan = raw_plan
    else:
        output_good = _mapping_value(row, "output_good", "good", "product")
        if output_good is None:
            raise ValueError(
                f"Предприятие {_normalise_id(row.get('id'), context='enterprise')!r}: "
                "скалярный plan требует output_good"
            )
        plan = {output_good: raw_plan}

    result: dict[str, float] = {}
    for raw_good, value in plan.items():
        good = str(raw_good)
        if good not in graph.good_index:
            raise KeyError(f"Неизвестный товар в production plan: {good}")
        result[good] = _number(value, context=f"План {good}")
    return result


def _fill_good_mapping(
    target: np.ndarray,
    row_index: int,
    raw_value: Any,
    graph: RecipeGraph,
    *,
    field: str,
) -> None:
    if raw_value is None:
        return
    if not isinstance(raw_value, Mapping):
        raise ValueError(f"{field}: требуется mapping good -> quantity")
    for raw_good, value in raw_value.items():
        good = str(raw_good)
        if good not in graph.good_index:
            raise KeyError(f"Неизвестный товар в {field}: {good}")
        target[row_index, graph.good_index[good]] = _number(
            value, context=f"{field}/{good}"
        )


def _capacity_for_row(
    row: Mapping[str, Any],
    plan: Mapping[str, float],
    target: np.ndarray,
    row_index: int,
    graph: RecipeGraph,
) -> None:
    raw_capacity = _mapping_value(row, "capacity_by_good", "capacities", "capacity")
    if raw_capacity is None:
        return
    if isinstance(raw_capacity, Mapping):
        _fill_good_mapping(target, row_index, raw_capacity, graph, field="capacity")
        return
    capacity = _number(raw_capacity, context="capacity", allow_infinity=True)
    # Скалярная мощность означает мощность заявленного выпуска. Для плана с
    # несколькими товарами это удобный краткий формат с одной мощностью на
    # каждый явно заявленный товар; точный вариант задаётся mapping-ом.
    for good in plan:
        target[row_index, graph.good_index[good]] = capacity


def _iter_transit(row: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Привести два распространённых формата поставок к последовательности строк."""

    raw = _mapping_value(row, "in_transit", "transit", "in_transit_supply", "deliveries")
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        records: list[Mapping[str, Any]] = []
        for raw_good, raw_records in raw.items():
            if isinstance(raw_records, Mapping):
                raw_records = [raw_records]
            elif not isinstance(raw_records, Iterable) or isinstance(raw_records, (str, bytes)):
                raw_records = [{"quantity": raw_records}]
            for record in raw_records:
                if not isinstance(record, Mapping):
                    raise ValueError("in_transit: запись должна быть mapping")
                records.append({"good": raw_good, **record})
        return records
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        raise ValueError("in_transit: требуется список записей или mapping")
    records = []
    for record in raw:
        if not isinstance(record, Mapping):
            raise ValueError("in_transit: запись должна быть mapping")
        records.append(record)
    return records


@dataclass(frozen=True)
class EnterpriseMatrices:
    """Числовой снимок предприятий, независимый от V3-объектов."""

    enterprise_ids: tuple[str, ...]
    good_ids: tuple[str, ...]
    production_plan: np.ndarray
    capacity: np.ndarray
    active: np.ndarray
    stock: np.ndarray
    production_reserved: np.ndarray
    other_reserved: np.ndarray
    strategic_stock: np.ndarray
    cash: np.ndarray
    cash_reserved: np.ndarray
    required_turn: np.ndarray
    transit_enterprise: np.ndarray
    transit_good: np.ndarray
    transit_quantity: np.ndarray
    transit_arrival_turn: np.ndarray
    transit_required_turn: np.ndarray

    def __post_init__(self) -> None:
        for name in (
            "production_plan",
            "capacity",
            "active",
            "stock",
            "production_reserved",
            "other_reserved",
            "strategic_stock",
            "cash",
            "cash_reserved",
            "required_turn",
            "transit_enterprise",
            "transit_good",
            "transit_quantity",
            "transit_arrival_turn",
            "transit_required_turn",
        ):
            getattr(self, name).setflags(write=False)

    @property
    def n_enterprises(self) -> int:
        return len(self.enterprise_ids)

    @property
    def n_goods(self) -> int:
        return len(self.good_ids)

    @property
    def enterprise_index(self) -> Mapping[str, int]:
        return {enterprise_id: index for index, enterprise_id in enumerate(self.enterprise_ids)}

    @property
    def free_cash(self) -> np.ndarray:
        """Свободные средства; денежные резервы не расходуются этим срезом."""

        return np.maximum(self.cash - self.cash_reserved, 0.0)

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, Any]],
        graph: RecipeGraph,
        *,
        current_turn: float = 0,
    ) -> "EnterpriseMatrices":
        """Нормализовать строки предприятий в отсортированные NumPy-таблицы."""

        normalised = []
        seen: set[str] = set()
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                raise ValueError("Строка предприятия должна быть mapping")
            row = dict(raw_row)
            enterprise_id = _normalise_id(row.get("id", row.get("enterprise_id")), context="enterprise")
            if enterprise_id in seen:
                raise ValueError(f"Повторный enterprise id: {enterprise_id}")
            seen.add(enterprise_id)
            row["id"] = enterprise_id
            normalised.append(row)
        normalised.sort(key=lambda row: row["id"])

        enterprise_ids = tuple(row["id"] for row in normalised)
        n_enterprises, n_goods = len(enterprise_ids), graph.n_goods
        production_plan = np.zeros((n_enterprises, n_goods), dtype=np.float64)
        capacity = np.full((n_enterprises, n_goods), np.inf, dtype=np.float64)
        active = np.ones(n_enterprises, dtype=bool)
        stock = np.zeros((n_enterprises, n_goods), dtype=np.float64)
        production_reserved = np.zeros((n_enterprises, n_goods), dtype=np.float64)
        other_reserved = np.zeros((n_enterprises, n_goods), dtype=np.float64)
        strategic_stock = np.zeros((n_enterprises, n_goods), dtype=np.float64)
        cash = np.zeros(n_enterprises, dtype=np.float64)
        cash_reserved = np.zeros(n_enterprises, dtype=np.float64)
        required_turns = np.full(n_enterprises, float(current_turn), dtype=np.float64)
        transit_rows: list[tuple[int, int, float, float, float]] = []

        for enterprise_index, row in enumerate(normalised):
            plan = _normalise_plan(row, graph)
            for good, quantity in plan.items():
                production_plan[enterprise_index, graph.good_index[good]] = quantity
            _capacity_for_row(row, plan, capacity, enterprise_index, graph)
            _fill_good_mapping(stock, enterprise_index, row.get("stock"), graph, field="stock")
            _fill_good_mapping(
                production_reserved,
                enterprise_index,
                _mapping_value(row, "production_reserved", "production_reservation"),
                graph,
                field="production_reserved",
            )
            _fill_good_mapping(
                other_reserved,
                enterprise_index,
                _mapping_value(
                    row,
                    "other_reserved",
                    "other_purpose_reservation",
                    "reserved_other",
                ),
                graph,
                field="other_reserved",
            )
            _fill_good_mapping(
                strategic_stock,
                enterprise_index,
                _mapping_value(row, "strategic_stock", "strategic_reserve"),
                graph,
                field="strategic_stock",
            )
            cash[enterprise_index] = _number(
                row.get("cash", row.get("balance", 0)), context="cash"
            )
            cash_reserved[enterprise_index] = _number(
                row.get("cash_reserved", row.get("reserved_cash", 0)),
                context="cash_reserved",
            )
            required_turns[enterprise_index] = _number(
                row.get("required_turn", current_turn), context="required_turn"
            )
            status = str(row.get("status", "active")).lower()
            active[enterprise_index] = bool(row.get("active", status not in {"closed", "inactive"}))
            if not active[enterprise_index]:
                capacity[enterprise_index, :] = 0.0

            for transit in _iter_transit(row):
                raw_good = transit.get("good", transit.get("good_id"))
                good = str(raw_good)
                if good not in graph.good_index:
                    raise KeyError(f"Неизвестный товар в in_transit: {good}")
                quantity = _number(
                    transit.get("quantity", transit.get("amount", 0)),
                    context=f"in_transit/{enterprise_ids[enterprise_index]}/{good}",
                )
                arrival_turn = _number(
                    transit.get("arrival_turn"), context="in_transit/arrival_turn"
                )
                transit_required_turn = _number(
                    transit.get("required_turn", required_turns[enterprise_index]),
                    context="in_transit/required_turn",
                )
                if quantity > _EPS:
                    transit_rows.append(
                        (
                            enterprise_index,
                            graph.good_index[good],
                            quantity,
                            arrival_turn,
                            transit_required_turn,
                        )
                    )

        transit_rows.sort(key=lambda item: (item[0], item[1], item[3], item[4]))
        if transit_rows:
            transit = np.asarray(transit_rows, dtype=np.float64)
            transit_enterprise = transit[:, 0].astype(np.int64)
            transit_good = transit[:, 1].astype(np.int64)
            transit_quantity = transit[:, 2]
            transit_arrival_turn = transit[:, 3]
            transit_required_turn = transit[:, 4]
        else:
            transit_enterprise = np.empty(0, dtype=np.int64)
            transit_good = np.empty(0, dtype=np.int64)
            transit_quantity = np.empty(0, dtype=np.float64)
            transit_arrival_turn = np.empty(0, dtype=np.float64)
            transit_required_turn = np.empty(0, dtype=np.float64)

        return cls(
            enterprise_ids=enterprise_ids,
            good_ids=graph.good_ids,
            production_plan=production_plan,
            capacity=capacity,
            active=active,
            stock=stock,
            production_reserved=production_reserved,
            other_reserved=other_reserved,
            strategic_stock=strategic_stock,
            cash=cash,
            cash_reserved=cash_reserved,
            required_turn=required_turns,
            transit_enterprise=transit_enterprise,
            transit_good=transit_good,
            transit_quantity=transit_quantity,
            transit_arrival_turn=transit_arrival_turn,
            transit_required_turn=transit_required_turn,
        )


@dataclass(frozen=True)
class InputDiagnostic:
    """Объяснимый итог одной пары предприятие/входной товар."""

    enterprise_id: str
    good_id: str
    required: float
    usable_stock: float
    due_supply: float
    late_supply: float
    order: float
    reason: str


@dataclass(frozen=True)
class EnterpriseDiagnostic:
    """Итог доступности выпуска предприятия."""

    enterprise_id: str
    status: str
    available_output: Mapping[str, float]
    limiting_good: str | None


@dataclass(frozen=True)
class ProductionResult:
    """Пакетный результат без побочных эффектов и без изменения входных массивов."""

    enterprise_ids: tuple[str, ...]
    good_ids: tuple[str, ...]
    required_inputs: np.ndarray
    usable_stock: np.ndarray
    due_supply: np.ndarray
    late_supply: np.ndarray
    available_inputs: np.ndarray
    input_orders: np.ndarray
    available_output: np.ndarray
    capacity_output: np.ndarray
    input_limited: np.ndarray
    input_reason_codes: np.ndarray
    enterprise_reason_codes: tuple[str, ...]
    input_diagnostics: tuple[InputDiagnostic, ...]
    enterprise_diagnostics: tuple[EnterpriseDiagnostic, ...]

    def __post_init__(self) -> None:
        for name in (
            "required_inputs",
            "usable_stock",
            "due_supply",
            "late_supply",
            "available_inputs",
            "input_orders",
            "available_output",
            "capacity_output",
            "input_limited",
            "input_reason_codes",
        ):
            getattr(self, name).setflags(write=False)

    @property
    def orders(self) -> np.ndarray:
        """Совместимое короткое имя для таблицы заявок на входы."""

        return self.input_orders


def calculate_production_orders(
    graph: RecipeGraph,
    enterprises: EnterpriseMatrices,
    *,
    detailed: bool = True,
) -> ProductionResult:
    """Рассчитать потребности предприятий и доступный выпуск.

    Формула свободного запаса: ``max(stock - other_reserved - strategic_stock,
    0)``. ``production_reserved`` намеренно не вычитается: это резерв,
    назначенный именно рассматриваемому плану. Поставки в пути учитываются
    только при ``arrival_turn <= required_turn``; поздние поставки остаются в
    отдельной диагностической таблице и не превращаются в текущий запас.
    """

    if enterprises.good_ids != graph.good_ids:
        raise ValueError("EnterpriseMatrices построены для другого набора товаров")

    plan = enterprises.production_plan
    required_inputs = plan @ graph.A.T
    usable_stock = np.maximum(
        enterprises.stock - enterprises.other_reserved - enterprises.strategic_stock,
        0.0,
    )
    due_supply = np.zeros_like(usable_stock)
    late_supply = np.zeros_like(usable_stock)
    if enterprises.transit_quantity.size:
        due_mask = enterprises.transit_arrival_turn <= enterprises.transit_required_turn
        np.add.at(
            due_supply,
            (enterprises.transit_enterprise[due_mask], enterprises.transit_good[due_mask]),
            enterprises.transit_quantity[due_mask],
        )
        np.add.at(
            late_supply,
            (enterprises.transit_enterprise[~due_mask], enterprises.transit_good[~due_mask]),
            enterprises.transit_quantity[~due_mask],
        )

    available_inputs = usable_stock + due_supply
    input_orders = np.maximum(required_inputs - available_inputs, 0.0)
    input_orders[input_orders < _EPS] = 0.0
    capacity_output = np.minimum(plan, enterprises.capacity)
    available_output = capacity_output.copy()
    input_limit_by_good = np.full_like(plan, np.inf)

    # Цикл идёт по типам товаров, а не по предприятиям. Это сохраняет
    # пакетную обработку главного измерения и допускает перенос на GPU.
    for output_index in range(graph.n_goods):
        ratios = graph.A[:, output_index]
        required_input_indices = np.flatnonzero(ratios > 0)
        if required_input_indices.size:
            ratios_for_output = ratios[required_input_indices]
            input_limit_by_good[:, output_index] = np.min(
                available_inputs[:, required_input_indices] / ratios_for_output[np.newaxis, :],
                axis=1,
            )
            available_output[:, output_index] = np.minimum(
                available_output[:, output_index], input_limit_by_good[:, output_index]
            )
    available_output[~enterprises.active, :] = 0.0
    input_limited = available_output + _EPS < capacity_output

    input_reason_codes = np.full(
        required_inputs.shape, "not_required", dtype=object
    )
    required_mask = required_inputs > _EPS
    covered_mask = required_mask & (input_orders <= _EPS)
    covered_due = covered_mask & (due_supply > _EPS) & (usable_stock + _EPS < required_inputs)
    input_reason_codes[covered_mask] = "covered_by_stock"
    input_reason_codes[covered_due] = "covered_by_due_delivery"
    order_mask = required_mask & (input_orders > _EPS)
    input_reason_codes[order_mask] = "order_required"
    order_after_due = order_mask & (due_supply > _EPS)
    input_reason_codes[order_after_due] = "order_required_after_due_delivery"
    order_late = order_mask & (late_supply > _EPS)
    input_reason_codes[order_late] = "order_required_late_delivery_ignored"

    enterprise_reasons: list[str] = []
    enterprise_diagnostics: list[EnterpriseDiagnostic] = []
    for enterprise_index, enterprise_id in enumerate(enterprises.enterprise_ids):
        if not enterprises.active[enterprise_index]:
            reason = "inactive_or_closed"
        elif not np.any(plan[enterprise_index] > _EPS):
            reason = "no_plan"
        elif np.any(input_limited[enterprise_index] & (plan[enterprise_index] > _EPS)):
            limited = np.flatnonzero(
                input_limited[enterprise_index] & (plan[enterprise_index] > _EPS)
            )
            reason = f"input_limited:{graph.good_ids[int(limited[0])] }"
        elif np.any(capacity_output[enterprise_index] + _EPS < plan[enterprise_index]):
            reason = "capacity_limited"
        else:
            reason = "plan_available"
        enterprise_reasons.append(reason)
        limiting = None
        planned = np.flatnonzero(plan[enterprise_index] > _EPS)
        if planned.size:
            limited = np.flatnonzero(input_limited[enterprise_index] & (plan[enterprise_index] > _EPS))
            if limited.size:
                limiting = graph.good_ids[int(limited[0])]
        available_map = {
            graph.good_ids[good_index]: float(available_output[enterprise_index, good_index])
            for good_index in planned
        }
        enterprise_diagnostics.append(
            EnterpriseDiagnostic(enterprise_id, reason, available_map, limiting)
        )

    input_diagnostics: list[InputDiagnostic] = []
    if detailed:
        for enterprise_index, enterprise_id in enumerate(enterprises.enterprise_ids):
            for good_index, good_id in enumerate(graph.good_ids):
                if required_inputs[enterprise_index, good_index] <= _EPS:
                    continue
                input_diagnostics.append(
                    InputDiagnostic(
                        enterprise_id=enterprise_id,
                        good_id=good_id,
                        required=float(required_inputs[enterprise_index, good_index]),
                        usable_stock=float(usable_stock[enterprise_index, good_index]),
                        due_supply=float(due_supply[enterprise_index, good_index]),
                        late_supply=float(late_supply[enterprise_index, good_index]),
                        order=float(input_orders[enterprise_index, good_index]),
                        reason=str(input_reason_codes[enterprise_index, good_index]),
                    )
                )

    return ProductionResult(
        enterprise_ids=enterprises.enterprise_ids,
        good_ids=graph.good_ids,
        required_inputs=required_inputs,
        usable_stock=usable_stock,
        due_supply=due_supply,
        late_supply=late_supply,
        available_inputs=available_inputs,
        input_orders=input_orders,
        available_output=available_output,
        capacity_output=capacity_output,
        input_limited=input_limited,
        input_reason_codes=input_reason_codes,
        enterprise_reason_codes=tuple(enterprise_reasons),
        input_diagnostics=tuple(input_diagnostics),
        enterprise_diagnostics=tuple(enterprise_diagnostics),
    )


def build_enterprise_matrices(
    rows: Iterable[Mapping[str, Any]],
    graph: RecipeGraph,
    *,
    current_turn: float = 0,
) -> EnterpriseMatrices:
    """Функциональный alias для построения числового контракта предприятий."""

    return EnterpriseMatrices.from_rows(rows, graph, current_turn=current_turn)


def calculate_enterprise_orders(
    enterprises: EnterpriseMatrices,
    graph: RecipeGraph,
    *,
    detailed: bool = True,
) -> ProductionResult:
    """Alias с естественным для вызывающего кода порядком аргументов."""

    return calculate_production_orders(graph, enterprises, detailed=detailed)


__all__ = [
    "EnterpriseDiagnostic",
    "EnterpriseMatrices",
    "InputDiagnostic",
    "ProductionResult",
    "build_enterprise_matrices",
    "calculate_enterprise_orders",
    "calculate_production_orders",
]
