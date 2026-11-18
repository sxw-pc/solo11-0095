"""删后可见行的精确整数统计披露：合同化最小隐藏与相容网格见证。

审计对账（diffs=0）后的可见行按 category×region 归并为精确整数网格。
披露只允许“隐藏”：可隐藏任意格子的精确人数、行合计或列合计；所有未隐藏
值必须等于删后可见行的真实计算值，不编造噪声。每个真实人数低于
mask_below 的敏感格，必须在所有相容非负整数网格（每格 0..CELL_BOUND）
中有至少 MIN_FEASIBLE_VALUES 个不同整数值可取，才视为不可精确反推。
合同在强制交付内容前提下最小化隐藏数量，同数按排序标识破平手；候选
枚举与可取值计算基于有界整数约束（带下界循环流精确判定），网格上限 3×4。
"""

from __future__ import annotations

from collections import deque
from itertools import combinations

CELL_BOUND = 20
MAX_ROWS = 3
MAX_COLUMNS = 4
MIN_FEASIBLE_VALUES = 3

POLICY_RETAIN_MARGINS = "retain_all_row_and_column_totals"
POLICY_RETAIN_CELLS = "retain_all_nonsensitive_cells"
KNOWN_POLICIES = (POLICY_RETAIN_MARGINS, POLICY_RETAIN_CELLS)


# ---------------------------------------------------------------------------
# 输入校验


def _validate_labels(value, kind, limit):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{kind} must be a non-empty list of labels")
    if len(value) > limit:
        raise ValueError(f"{kind} supports at most {limit} labels, got {len(value)}")
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item or ":" in item:
            raise ValueError(f"{kind} label must be a non-empty string without ':': {item!r}")
        if item in seen:
            raise ValueError(f"duplicate {kind} label: {item}")
        seen.add(item)
    return list(value)


def validate_grid_spec(rows, columns, mask_below):
    """校验行列标签与阈值；网格最大 3×4。"""
    rows = _validate_labels(rows, "rows", MAX_ROWS)
    columns = _validate_labels(columns, "columns", MAX_COLUMNS)
    if isinstance(mask_below, bool) or not isinstance(mask_below, int) or mask_below < 1:
        raise ValueError("mask_below must be an integer >= 1")
    return rows, columns, mask_below


def validate_counts(counts, rows, columns, cell_bound=CELL_BOUND):
    """校验精确人数表：每格必须是 0..cell_bound 的整数且恰好覆盖声明网格。"""
    if not isinstance(counts, dict):
        raise ValueError("counts must be a mapping of row label to column counts")
    if set(counts) != set(rows):
        raise ValueError("counts rows do not match the declared grid rows")
    table = {}
    for row in rows:
        row_counts = counts[row]
        if not isinstance(row_counts, dict) or set(row_counts) != set(columns):
            raise ValueError(f"counts for row {row!r} do not match the declared grid columns")
        table[row] = {}
        for column in columns:
            value = row_counts[column]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"count for {row}:{column} must be an integer, got {value!r}")
            if value < 0 or value > cell_bound:
                raise ValueError(f"count for {row}:{column} must be within 0..{cell_bound}, got {value}")
            table[row][column] = value
    return table


def validate_contracts(contracts):
    if not isinstance(contracts, dict) or not contracts:
        raise ValueError("contracts must be a non-empty mapping of name to policy")
    for name, policy in contracts.items():
        if not isinstance(name, str) or not name:
            raise ValueError("contract names must be non-empty strings")
        if policy not in KNOWN_POLICIES:
            raise ValueError(f"unknown contract policy for {name!r}: {policy!r}")
    return dict(contracts)


# ---------------------------------------------------------------------------
# 标识符


def _cell_id(cell):
    return f"{cell[0]}:{cell[1]}"


def _item_id(item):
    kind, value = item
    if kind == "cell":
        return _cell_id(value)
    return f"{kind}:{value}"


def _hidden_identifiers(hidden_cells, hidden_rows, hidden_columns):
    ids = [_cell_id(cell) for cell in hidden_cells]
    ids += [f"row:{row}" for row in hidden_rows]
    ids += [f"column:{column}" for column in hidden_columns]
    return sorted(ids)


# ---------------------------------------------------------------------------
# 有界整数可行性：带下界循环流


def _max_flow(nodes, arcs, source, sink):
    """Edmonds-Karp；arcs 为 (u, v, capacity)。"""
    capacity = {node: {} for node in nodes}
    for u, v, amount in arcs:
        if amount <= 0:
            continue
        capacity[u][v] = capacity[u].get(v, 0) + amount
        capacity[v].setdefault(u, 0)
    total = 0
    while True:
        parent = {source: None}
        queue = deque([source])
        while queue and sink not in parent:
            node = queue.popleft()
            for nxt, amount in capacity[node].items():
                if amount > 0 and nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        if sink not in parent:
            return total
        increment = None
        node = sink
        while node != source:
            prev = parent[node]
            amount = capacity[prev][node]
            increment = amount if increment is None else min(increment, amount)
            node = prev
        node = sink
        while node != source:
            prev = parent[node]
            capacity[prev][node] -= increment
            capacity[node][prev] = capacity[node].get(prev, 0) + increment
            node = prev
        total += increment


def _circulation_feasible(nodes, arcs):
    """判断带下界的循环流是否可行；arcs 为 (u, v, lower, upper)。"""
    balance = {node: 0 for node in nodes}
    plain = []
    for u, v, lower, upper in arcs:
        if lower > upper:
            return False
        plain.append((u, v, upper - lower))
        balance[u] -= lower
        balance[v] += lower
    super_source = ("super_source",)
    super_sink = ("super_sink",)
    demand = 0
    for node, amount in balance.items():
        if amount > 0:
            plain.append((super_source, node, amount))
            demand += amount
        elif amount < 0:
            plain.append((node, super_sink, -amount))
    all_nodes = list(nodes) + [super_source, super_sink]
    return _max_flow(all_nodes, plain, super_source, super_sink) == demand


class _GridProblem:
    """一张精确人数表及其行列合计；判定“隐藏集合”下的相容网格。"""

    def __init__(self, rows, columns, counts, cell_bound=CELL_BOUND):
        self.rows = rows
        self.columns = columns
        self.counts = counts
        self.cell_bound = cell_bound
        self.row_totals = {row: sum(counts[row][col] for col in columns) for row in rows}
        self.column_totals = {col: sum(counts[row][col] for row in rows) for col in columns}
        self.cells = [(row, col) for row in rows for col in columns]

    def _network(self, hidden_cells, hidden_rows, hidden_columns, fixed):
        hidden = set(hidden_cells)
        source, sink = ("source",), ("sink",)
        nodes = [source, sink]
        nodes += [("row", row) for row in self.rows]
        nodes += [("column", col) for col in self.columns]
        big = self.cell_bound * len(self.cells) + 1
        arcs = []
        for row in self.rows:
            node = ("row", row)
            degree = sum(1 for col in self.columns if (row, col) in hidden)
            if row in hidden_rows:
                arcs.append((source, node, 0, self.cell_bound * degree))
            else:
                residual = self.row_totals[row] - sum(
                    self.counts[row][col] for col in self.columns if (row, col) not in hidden
                )
                if residual < 0 or residual > self.cell_bound * degree:
                    return None
                arcs.append((source, node, residual, residual))
        for col in self.columns:
            node = ("column", col)
            degree = sum(1 for row in self.rows if (row, col) in hidden)
            if col in hidden_columns:
                arcs.append((node, sink, 0, self.cell_bound * degree))
            else:
                residual = self.column_totals[col] - sum(
                    self.counts[row][col] for row in self.rows if (row, col) not in hidden
                )
                if residual < 0 or residual > self.cell_bound * degree:
                    return None
                arcs.append((node, sink, residual, residual))
        for row, col in sorted(hidden):
            node_u, node_v = ("row", row), ("column", col)
            if (row, col) in fixed:
                value = fixed[(row, col)]
                arcs.append((node_u, node_v, value, value))
            else:
                arcs.append((node_u, node_v, 0, self.cell_bound))
        arcs.append((sink, source, 0, big))
        return nodes, arcs

    def feasible(self, hidden_cells, hidden_rows, hidden_columns, fixed=None):
        """存在与已发布精确值/边际相容的 0..cell_bound 整数网格。"""
        built = self._network(hidden_cells, hidden_rows, hidden_columns, fixed or {})
        if built is None:
            return False
        nodes, arcs = built
        return _circulation_feasible(nodes, arcs)

    def feasible_values(self, cell, hidden_cells, hidden_rows, hidden_columns):
        """该格在所有相容网格中的可取值集合（已发布的格子只有真实值）。"""
        if cell not in hidden_cells:
            return [self.counts[cell[0]][cell[1]]]
        return [
            value
            for value in range(self.cell_bound + 1)
            if self.feasible(hidden_cells, hidden_rows, hidden_columns, {cell: value})
        ]


# ---------------------------------------------------------------------------
# 候选搜索与隐私判定


def _privacy_failures(problem, sensitive_cells, hidden_cells, hidden_rows, hidden_columns):
    failures = []
    for cell in sensitive_cells:
        values = problem.feasible_values(cell, hidden_cells, hidden_rows, hidden_columns)
        if len(values) < MIN_FEASIBLE_VALUES:
            failures.append({"cell": _cell_id(cell), "feasible_values": values})
    return failures


def _decode(combo):
    cells = frozenset(value for kind, value in combo if kind == "cell")
    rows = frozenset(value for kind, value in combo if kind == "row")
    columns = frozenset(value for kind, value in combo if kind == "column")
    return cells, rows, columns


def _free_items(problem, policy, base_cells):
    """合同强制交付之外、可以自由选择隐藏的项目。

    敏感格必须隐藏（已发布的敏感格只有真实值一个可取值，必然不合格），
    因此作为 base 不计入自由项。
    """
    if policy == POLICY_RETAIN_MARGINS:
        items = [("cell", cell) for cell in problem.cells if cell not in base_cells]
    elif policy == POLICY_RETAIN_CELLS:
        items = [("row", row) for row in problem.rows]
        items += [("column", col) for col in problem.columns]
    else:
        raise ValueError(f"unknown contract policy: {policy}")
    return sorted(items, key=_item_id)


def _plan_contract(problem, name, policy, sensitive_cells):
    base_cells = frozenset(sensitive_cells)
    free_items = _free_items(problem, policy, base_cells)
    rejected = []
    for size in range(len(free_items) + 1):
        for combo in combinations(free_items, size):
            extra_cells, hidden_rows, hidden_columns = _decode(combo)
            hidden_cells = base_cells | extra_cells
            failures = _privacy_failures(
                problem, sensitive_cells, hidden_cells, hidden_rows, hidden_columns
            )
            if not failures:
                return _issued_plan(
                    problem, policy, sensitive_cells, hidden_cells,
                    hidden_rows, hidden_columns, rejected,
                )
            rejected.append({
                "hidden": _hidden_identifiers(hidden_cells, hidden_rows, hidden_columns),
                "failures": failures,
            })
    return _refused_plan(policy, sensitive_cells, rejected)


def _issued_plan(problem, policy, sensitive_cells, hidden_cells, hidden_rows,
                 hidden_columns, rejected):
    sensitive_values = {
        _cell_id(cell): problem.feasible_values(cell, hidden_cells, hidden_rows, hidden_columns)
        for cell in sensitive_cells
    }
    witnesses = _build_witnesses(problem, sensitive_cells, hidden_cells, hidden_rows, hidden_columns)
    hidden_count = len(hidden_cells) + len(hidden_rows) + len(hidden_columns)
    smaller = sum(1 for item in rejected if len(item["hidden"]) < hidden_count)
    if policy == POLICY_RETAIN_MARGINS:
        delivered = "all row and column totals"
        hidden_desc = f"{len(hidden_cells)} cell count(s)"
    else:
        delivered = "all non-sensitive cell counts"
        hidden_desc = (
            f"{len(hidden_rows) + len(hidden_columns)} marginal total(s) "
            f"plus {len(hidden_cells)} sensitive cell(s)"
        )
    return {
        "status": "issued",
        "policy": policy,
        "hidden_count": hidden_count,
        "hidden_cells": [_cell_id(cell) for cell in sorted(hidden_cells)],
        "hidden_row_totals": sorted(hidden_rows),
        "hidden_column_totals": sorted(hidden_columns),
        "published_cells": {
            _cell_id((row, col)): problem.counts[row][col]
            for row, col in problem.cells
            if (row, col) not in hidden_cells
        },
        "published_row_totals": {
            row: problem.row_totals[row] for row in problem.rows if row not in hidden_rows
        },
        "published_column_totals": {
            col: problem.column_totals[col] for col in problem.columns if col not in hidden_columns
        },
        "sensitive_cell_values": sensitive_values,
        "witnesses": witnesses,
        "certificate": {
            "exhaustive": True,
            "chosen_hidden_count": hidden_count,
            "smaller_candidate_count": smaller,
            "rejected_candidates": rejected,
            "sensitive_cells_always_hidden": [_cell_id(cell) for cell in sensitive_cells],
            "tie_break": "fewest hidden items, then lexicographically smallest sorted identifiers",
        },
        "reason": (
            f"issued: delivers {delivered}; hides {hidden_desc}, the minimum so every "
            f"sensitive cell has at least {MIN_FEASIBLE_VALUES} feasible values"
        ),
    }


def _refused_plan(policy, sensitive_cells, rejected):
    maximal = rejected[-1]
    worst = maximal["failures"][0]
    return {
        "status": "refused",
        "policy": policy,
        "reason": (
            f"refused: even hiding every permitted item leaves sensitive cell "
            f"{worst['cell']} with only {len(worst['feasible_values'])} feasible value(s); "
            f"no publishable scheme reaches {MIN_FEASIBLE_VALUES}"
        ),
        "certificate": {
            "exhaustive": True,
            "checked_candidates": len(rejected),
            "maximal_candidate": maximal,
            "sensitive_cells_always_hidden": [_cell_id(cell) for cell in sensitive_cells],
            "monotone_note": (
                "hiding more items only enlarges the compatible grid set, so no subset "
                "of the maximal candidate can succeed either"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 相容网格见证


def _witness_values(true_value, feasible_values, cell_bound):
    """优先取真实值及其最近的两侧值，凑足 MIN_FEASIBLE_VALUES 个。"""
    feasible = set(feasible_values)
    chosen = []
    for distance in range(cell_bound + 1):
        candidates = (true_value,) if distance == 0 else (true_value - distance, true_value + distance)
        for candidate in candidates:
            if 0 <= candidate <= cell_bound and candidate in feasible and candidate not in chosen:
                chosen.append(candidate)
        if len(chosen) >= MIN_FEASIBLE_VALUES:
            break
    return sorted(chosen[:MIN_FEASIBLE_VALUES])


def _complete_grid(problem, hidden_cells, hidden_rows, hidden_columns, preset):
    """在预设敏感格取值后，按排序标识逐格取最小可行值补全整张网格。"""
    fixed = dict(preset)
    for cell in sorted(hidden_cells):
        if cell in fixed:
            continue
        for value in range(problem.cell_bound + 1):
            fixed[cell] = value
            if problem.feasible(hidden_cells, hidden_rows, hidden_columns, fixed):
                break
        else:
            raise ValueError("no compatible completion for witness grid")
    return {
        row: {
            col: (fixed[(row, col)] if (row, col) in hidden_cells else problem.counts[row][col])
            for col in problem.columns
        }
        for row in problem.rows
    }


def _build_witnesses(problem, sensitive_cells, hidden_cells, hidden_rows, hidden_columns):
    witnesses = []
    seen = set()
    for cell in sensitive_cells:
        values = problem.feasible_values(cell, hidden_cells, hidden_rows, hidden_columns)
        true_value = problem.counts[cell[0]][cell[1]]
        for value in _witness_values(true_value, values, problem.cell_bound):
            grid = _complete_grid(problem, hidden_cells, hidden_rows, hidden_columns, {cell: value})
            key = tuple(tuple(grid[row][col] for col in problem.columns) for row in problem.rows)
            if key in seen:
                continue
            seen.add(key)
            witnesses.append({
                "grid": grid,
                "sensitive_values": {
                    _cell_id(target): grid[target[0]][target[1]] for target in sensitive_cells
                },
            })
    return witnesses


# ---------------------------------------------------------------------------
# 披露计划与签发


def plan_disclosure(counts, rows, columns, mask_below, contracts, cell_bound=CELL_BOUND):
    """对一张精确人数表逐合同求最小隐藏披露计划（纯函数，不触碰审计）。"""
    rows, columns, mask_below = validate_grid_spec(rows, columns, mask_below)
    counts = validate_counts(counts, rows, columns, cell_bound)
    contracts = validate_contracts(contracts)
    problem = _GridProblem(rows, columns, counts, cell_bound)
    sensitive_cells = sorted(
        ((row, col) for row in rows for col in columns if counts[row][col] < mask_below),
        key=_cell_id,
    )
    plans = {
        name: _plan_contract(problem, name, policy, sensitive_cells)
        for name, policy in sorted(contracts.items())
    }
    return {
        "rows": rows,
        "columns": columns,
        "counts": counts,
        "row_totals": problem.row_totals,
        "column_totals": problem.column_totals,
        "mask_below": mask_below,
        "cell_bound": cell_bound,
        "min_feasible_values": MIN_FEASIBLE_VALUES,
        "sensitive_cells": [_cell_id(cell) for cell in sensitive_cells],
        "contracts": plans,
    }


def build_release(data, result):
    """把披露计划绑定到一次已对账审计：快照、run_key、diffs 全部核对。"""
    if (
        result.workspace.snapshot.snapshot_id != data.get("snapshot_id")
        or result.run_key != data.get("run_key")
    ):
        raise ValueError("attendance release is bound to a different audit")
    if result.stats["diffs"] != 0:
        raise ValueError("unreconciled visible rows cannot be released")
    rows, columns, mask_below = validate_grid_spec(
        data.get("rows"), data.get("columns"), data.get("mask_below")
    )
    counts = {row: {column: 0 for column in columns} for row in rows}
    visible = {decision.key for decision in result.decisions if decision.visible}
    for record in result.workspace.rows:
        if (record.file_id, record.row_pos) not in visible:
            continue
        category = record.values.get("category")
        region = record.values.get("region")
        if category not in counts or region not in counts.get(category, {}):
            raise ValueError(f"visible record outside declared grid: {category!r}/{region!r}")
        counts[category][region] += 1
    plan = plan_disclosure(counts, rows, columns, mask_below, data.get("contracts"))
    return {
        "snapshot_id": data["snapshot_id"],
        "run_key": result.run_key,
        "diffs": result.stats["diffs"],
        "visible_rows": result.stats["visible_rows"],
        "hidden_rows": result.stats["hidden_rows"],
        **plan,
    }
