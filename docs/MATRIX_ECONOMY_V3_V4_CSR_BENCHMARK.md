# Benchmark V3 vs V4 CSR

Измерены свежие копии одного seed: 10 seeds, 1 прогрев и 10 тёплых повторов, порядок V3/V4 чередуется. Создание мира вне таймеров. Полный pytest не запускался.

## Сопоставимость

V3 выполняет реальный `basic needs local_only` и общий matcher с применением сделок. V4 выполняет shadow snapshot, route enumeration, CSR max-flow и попытку того же `commit_trade_atomic`. На контрольном seed применение CSR завершилось ValueError из-за остатка товара; поэтому V4 trade total ниже — стоимость shadow-пути до/во время попытки применения, не завершённая торговая фаза. V4 не является полным месячным ходом: production plans, adaptive top-up и V3 observations не подключены к V4 runtime.

## Медиана по seed, warm repeats

| Seed | V3 trade ms | V4 CSR trade ms | V3/V4 |
|---:|---:|---:|---:|
| 7 | 78.141 | 321.746 | 0.243x |
| 17 | 84.274 | 374.156 | 0.225x |
| 43 | 75.860 | 298.969 | 0.254x |
| 101 | 80.766 | 305.108 | 0.265x |
| 2024 | 79.644 | 481.518 | 0.165x |
| 4231 | 79.671 | 409.773 | 0.194x |
| 5150 | 80.971 | 345.571 | 0.234x |
| 9001 | 81.657 | 333.079 | 0.245x |
| 31415 | 80.408 | 290.994 | 0.276x |
| 73129 | 78.769 | 325.848 | 0.242x |

Общая медиана парных seed ratios V3/V4: **0.242x**; разброс min–max `0.165…0.276`.

## Стадии

| Этап | V3 median ms | V4 CSR median ms | V3/V4 | V3 p95 ms | V4 p95 ms |
|---|---:|---:|---:|---:|---:|
| shared_preparation_ns | 588.915 | 634.938 | 0.928x | 864.539 | 963.996 |
| routes_landed_ns | 3.510 | 43.868 | 0.080x | 4.469 | 404.822 |
| route_search_ns | 32.781 | 95.345 | 0.344x | 37.511 | 203.877 |
| snapshot_ns | 0.000 | 93.973 | 0.000x | 0.000 | 115.518 |
| csr_build_ns | 0.000 | 7.869 | 0.000x | 0.000 | 9.666 |
| distribution_ns | 2.747 | 8.415 | 0.326x | 3.807 | 10.313 |
| apply_ns | 30.461 | 20.339 | 1.498x | 36.516 | 27.677 |
| finalization_ns | 10.983 | 1.764 | 6.226x | 12.507 | 3.252 |
| trade_total_ns | 79.959 | 320.355 | 0.250x | 97.825 | 692.582 |

| Производство (неэквивалентные политики) | V3 plan_enterprises | V4 matrix calculation |
|---|---:|---:|
| median ms | 60.239 | 4.777 |

Production is not assigned a speedup: V3 `plan_enterprises` and V4 matrix calculation do not implement the same economic policy. Their raw medians are retained in CSV (`v3_plan_ns` vs `v4_matrix_ns`) for reference only.

## Workload and correctness checks

The benchmark records buy/sell counts, CSR pairs, volume, trade count and state error count per run. The input fingerprint must match between V3 and V4 for each seed/repeat; mismatches abort the run. Different trade volume or trade count is expected because V4 lacks V3 local-only/adaptive behavior and is not an equivalent full-month implementation.

Median produced total: V3 `0.000`, V4 matrix `12090.224`; this is a policy/output mismatch, not a speed comparison.
Paired post-phase aggregate balance mismatches: gold `0`, stock `0` at tolerance `1e-7`.

## Conclusion

CSR application failed in 60 of 100 warm runs; only 40 completed. A valid full trade-phase or whole-economy speedup cannot be reported: CSR does not reliably apply its allocation, and V4 production/runtime phases are not integrated. The tables retain measured shadow-path and solver costs, but no V4/V3 acceleration coefficient is claimed.

