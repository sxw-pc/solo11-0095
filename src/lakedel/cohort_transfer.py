"""Bind a pre-deletion inspection draw to two verified lake snapshots and decide quality publication.

Two pre-registered contracts are evaluated against the verified transfer evidence:

- ``unconditional_portable`` carries the unconditional coverage guarantee of the
  fixed pre-deletion random design conservatively to the deletion-only after
  population.  Each original region gets alpha = 1/40 (0.025), so two regions
  give at least 19/20 (95%) simultaneous coverage.  Per-region defect counts
  are bounded by exact hypergeometric inversion against the *before* frame, and
  a bound is reduced only by deleted rows whose inspection receipt confirms a
  defect.  The carried bounds are valid but conservative; they are not claimed
  to be the unique tightest bounds.
- ``fresh_current_draw`` requires a random draw executed after the current
  visible set formed, with new receipts.  Receipts produced before deletion do
  not satisfy this service commitment even when every inspected row survives;
  the contract then holds (it does not report a quality defect, and it does
  not claim re-sampling is the only statistically valid conditional inference).

Both contracts gate on the same quality bar: the one-sided 95% upper bound of
the current visible population defect rate must not exceed the threshold
(default 1/4).
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from fractions import Fraction
from math import comb
from pathlib import Path

from .engine import execute_audit
from .util import canonical_json, sha256_text

PRE_DELETION_DESIGN = "srswor_by_region_before_deletion"
ALPHA_PER_REGION = Fraction(1, 40)  # 0.025 per original region
DEFAULT_QUALITY_THRESHOLD = Fraction(1, 4)


def hypergeometric_tail_leq(population: int, defects: int, draws: int, observed: int) -> Fraction:
    """Exact P(X <= observed) for X ~ Hypergeometric(population, defects, draws)."""
    if not (0 <= observed <= draws <= population and 0 <= defects <= population):
        raise ValueError("hypergeometric parameters out of range")
    total = comb(population, draws)
    hits = 0
    for k in range(0, min(observed, defects) + 1):
        if draws - k <= population - defects:
            hits += comb(defects, k) * comb(population - defects, draws - k)
    return Fraction(hits, total)


def hypergeometric_upper_bound(population: int, draws: int, observed: int, alpha: Fraction) -> int:
    """Largest defect count D with P(X <= observed | D) >= alpha (exact inversion)."""
    if not (0 <= observed <= draws <= population):
        raise ValueError("sample counts out of range")
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie in (0, 1)")
    bound = observed
    for defects in range(observed + 1, population - draws + observed + 1):
        if hypergeometric_tail_leq(population, defects, draws, observed) >= alpha:
            bound = defects
    return bound


def _decimal_str(value: Fraction, places: int = 12) -> str:
    scale = 10**places
    quotient, remainder = divmod(value.numerator * scale, value.denominator)
    if remainder * 2 >= value.denominator:
        quotient += 1
    whole, frac = divmod(quotient, scale)
    return f"{whole}.{frac:0{places}d}"


def _visible_rows(audit):
    if audit.diffs or any(item.severity == "error" for item in audit.findings):
        raise ValueError("snapshot audit is not clean")
    rows = {
        row.row_id: row
        for row, decision in zip(audit.workspace.rows, audit.decisions)
        if decision.visible and row.row_id is not None
    }
    if len(rows) != audit.stats["visible_rows"]:
        raise ValueError("visible row identity is missing or duplicated")
    return rows


def _portable_evaluation(evidence: dict, threshold: Fraction) -> dict:
    """Evaluate the unconditional_portable contract on verified transfer evidence."""
    design = evidence["design"]
    if design != PRE_DELETION_DESIGN:
        return {
            "bounds": None,
            "decision": {
                "status": "reject",
                "reasons": [
                    f"design {design!r} is not the pre-registered pre-deletion fixed design; "
                    "the unconditional carry requires a fixed random draw executed before deletion"
                ],
                "certificate": None,
            },
        }
    inspections = evidence["inspections"]
    region_of = {item["row_id"]: item["region"] for item in inspections}
    defect_rows = {item["row_id"] for item in inspections if item["defect"]}
    confirmed_deleted = Counter(
        region_of[row_id] for row_id in set(evidence["deleted_row_ids"]) & defect_rows
    )
    regions: dict[str, dict] = {}
    total_bound = 0
    for region in sorted(evidence["population_before_by_region"]):
        population = evidence["population_before_by_region"][region]
        draws = evidence["sample_by_region"][region]
        observed = evidence["sample_defects_by_region"].get(region, 0)
        bound = hypergeometric_upper_bound(population, draws, observed, ALPHA_PER_REGION)
        tail_at = hypergeometric_tail_leq(population, bound, draws, observed)
        feasible_max = population - draws + observed
        tail_above = (
            hypergeometric_tail_leq(population, bound + 1, draws, observed)
            if bound < feasible_max
            else None
        )
        deducted = confirmed_deleted.get(region, 0)
        carried = bound - deducted
        regions[region] = {
            "population_before": population,
            "sample_size": draws,
            "observed_defects": observed,
            "alpha": str(ALPHA_PER_REGION),
            "defect_bound_before": bound,
            "tail_at_bound": str(tail_at),
            "tail_above_bound": str(tail_above) if tail_above is not None else None,
            "confirmed_deleted_defects": deducted,
            "carried_bound_after": carried,
            "surviving_population": evidence["population_after_by_region"].get(region, 0),
        }
        total_bound += carried
    population_after = sum(evidence["population_after_by_region"].values())
    rate = Fraction(total_bound, population_after)
    coverage = 1 - ALPHA_PER_REGION * len(regions)
    bounds = {
        "defects_bound": total_bound,
        "population": population_after,
        "rate": str(rate),
        "rate_decimal": _decimal_str(rate),
        "regions": regions,
    }
    shared_reasons = [
        "after frame verified as a deletion-only descendant of the sampled before frame "
        "(strict subset, surviving row content unchanged)",
        f"per-region alpha {ALPHA_PER_REGION} over {len(regions)} regions gives simultaneous "
        f"coverage at least {coverage}",
    ]
    if rate <= threshold:
        reasons = shared_reasons + [
            f"carried defect bound {total_bound} over current visible population "
            f"{population_after} gives rate bound {rate} within gate {threshold}"
        ]
        certificate = {
            "contract": "unconditional_portable",
            "guarantee_basis": "unconditional coverage of the pre-deletion fixed random "
            "design, carried conservatively to the deletion-only after population",
            "bound_nature": "conservative valid bound from exact hypergeometric inversion; "
            "not asserted to be the unique tightest bound",
            "simultaneous_coverage": str(coverage),
            "alpha_by_region": {region: str(ALPHA_PER_REGION) for region in sorted(regions)},
            "before_snapshot": evidence["before_snapshot"],
            "after_snapshot": evidence["after_snapshot"],
            "before_run_key": evidence["before_run_key"],
            "after_run_key": evidence["after_run_key"],
            "population_after": population_after,
            "defects_bound": total_bound,
            "rate_bound": str(rate),
            "rate_bound_decimal": _decimal_str(rate),
            "threshold": str(threshold),
            "region_witness": regions,
        }
        return {"bounds": bounds, "decision": {"status": "publish", "reasons": reasons, "certificate": certificate}}
    reasons = shared_reasons + [
        f"carried defect bound {total_bound} over current visible population "
        f"{population_after} gives rate bound {rate} above gate {threshold}; publication blocked"
    ]
    return {
        "bounds": bounds,
        "decision": {"status": "hold", "reasons": reasons, "certificate": None},
    }


def _fresh_evaluation(evidence: dict, threshold: Fraction) -> dict:
    """Evaluate the fresh_current_draw contract on verified transfer evidence."""
    design = evidence["design"]
    if design == PRE_DELETION_DESIGN:
        population_after = sum(evidence["population_after_by_region"].values())
        receipt_count = len(evidence["inspections"])
        return {
            "bounds": None,
            "decision": {
                "status": "hold",
                "reasons": [
                    f"draw design {design!r} produced all {receipt_count} inspection receipts "
                    "before the current visible set formed",
                    f"contract requires a fresh random draw from the current "
                    f"{population_after}-row visible frame with new receipts; surviving "
                    "pre-deletion receipts do not satisfy this service commitment",
                    "hold reflects evidence timing only, not an observed quality defect, and "
                    "does not claim re-sampling is the only statistically valid conditional "
                    "inference",
                ],
                "certificate": None,
            },
        }
    return {
        "bounds": None,
        "decision": {
            "status": "reject",
            "reasons": [
                f"design {design!r} is not a verifiable post-formation draw design; cannot "
                "confirm receipts were drawn from the audited current visible frame"
            ],
            "certificate": None,
        },
    }


def evaluate_quality_contracts(evidence: dict, threshold: Fraction = DEFAULT_QUALITY_THRESHOLD) -> dict:
    """Evaluate both pre-registered publication contracts against verified evidence."""
    portable = _portable_evaluation(evidence, threshold)
    fresh = _fresh_evaluation(evidence, threshold)
    region_count = len(evidence["population_before_by_region"])
    return {
        "population_upper_bounds": {
            "semantics": "one-sided upper confidence bounds on defects in the current "
            "visible population; null when a contract cannot issue a bound",
            "unconditional_portable": portable["bounds"],
            "fresh_current_draw": fresh["bounds"],
        },
        "publication_decision": {
            "gate": {
                "metric": "current visible population defect rate",
                "sided": "one-sided upper",
                "confidence": str(1 - ALPHA_PER_REGION * region_count),
                "threshold": str(threshold),
                "threshold_decimal": _decimal_str(threshold),
            },
            "contracts": {
                "unconditional_portable": portable["decision"],
                "fresh_current_draw": fresh["decision"],
            },
        },
    }


def inspect_deletion_cohort(
    root: str | Path,
    draw_path: str | Path,
    quality_threshold: Fraction | str = DEFAULT_QUALITY_THRESHOLD,
) -> dict:
    """Verify the transfer evidence and decide quality publication per contract."""
    threshold = Fraction(quality_threshold)
    if not 0 < threshold < 1:
        raise ValueError("quality threshold must lie in (0, 1)")
    root = Path(root)
    draw = json.loads(Path(draw_path).read_text(encoding="utf-8"))
    if draw.get("design") != PRE_DELETION_DESIGN:
        raise ValueError("unsupported pre-deletion draw design")
    before = execute_audit(root / "before")
    after = execute_audit(root / "after")
    if (draw.get("before_snapshot"), draw.get("after_snapshot")) != (
        before.workspace.snapshot.snapshot_id, after.workspace.snapshot.snapshot_id
    ):
        raise ValueError("draw snapshot binding does not match audits")
    before_rows = _visible_rows(before)
    after_rows = _visible_rows(after)
    if not set(after_rows) < set(before_rows):
        raise ValueError("after frame must be a strict subset of before frame")
    if any(after_rows[row_id].values != before_rows[row_id].values for row_id in after_rows):
        raise ValueError("surviving row content changed across snapshots")
    if set(row.row_id for row in before.workspace.rows) != set(row.row_id for row in after.workspace.rows):
        raise ValueError("underlying row identities changed across snapshots")

    seed = draw.get("seed")
    sizes = draw.get("sample_size_by_region")
    inspections = draw.get("inspections")
    if type(seed) is not int or not isinstance(sizes, dict) or not isinstance(inspections, list):
        raise ValueError("draw seed, sizes or inspections invalid")
    by_region: dict[str, list[str]] = {}
    for row_id, row in before_rows.items():
        by_region.setdefault(row.values["region"], []).append(row_id)
    if set(sizes) != set(by_region):
        raise ValueError("draw regions do not match before frame")
    rng = random.Random(seed)
    selected = set()
    for region, row_ids in sorted(by_region.items()):
        count = sizes[region]
        if type(count) is not int or not 0 < count <= len(row_ids):
            raise ValueError("invalid region sample size")
        selected.update(rng.sample(sorted(row_ids), count))
    seen = set()
    observed = []
    for item in inspections:
        if not isinstance(item, dict):
            raise ValueError("inspection entry invalid")
        row_id, defect = item.get("row_id"), item.get("defect")
        if not isinstance(row_id, str) or row_id in seen or type(defect) is not bool:
            raise ValueError("inspection receipt invalid or duplicated")
        seen.add(row_id)
        if row_id not in selected:
            raise ValueError("inspection IDs do not match fixed random draw")
        observed.append({"row_id": row_id, "region": before_rows[row_id].values["region"], "defect": defect})
    if seen != selected:
        raise ValueError("inspection IDs do not match fixed random draw")
    deleted = set(before_rows) - set(after_rows)
    if draw.get("deletion_policy") == "lowest_five_unselected_east_ids_after_draw":
        expected = set(sorted(set(by_region["east"]) - selected)[:5])
        if deleted != expected:
            raise ValueError("deletion does not match declared post-draw policy")
    else:
        raise ValueError("unsupported deletion policy")
    payload = {
        "before_snapshot": before.workspace.snapshot.snapshot_id,
        "after_snapshot": after.workspace.snapshot.snapshot_id,
        "before_run_key": before.run_key,
        "after_run_key": after.run_key,
        "design": draw["design"],
        "deletion_policy": draw["deletion_policy"],
        "population_before_by_region": dict(sorted(Counter(row.values["region"] for row in before_rows.values()).items())),
        "population_after_by_region": dict(sorted(Counter(row.values["region"] for row in after_rows.values()).items())),
        "sample_by_region": dict(sorted(Counter(item["region"] for item in observed).items())),
        "sample_defects_by_region": dict(sorted(Counter(item["region"] for item in observed if item["defect"]).items())),
        "deleted_row_ids": sorted(deleted),
        "deleted_inspected_count": len(deleted & selected),
        "surviving_inspected_count": len(selected & set(after_rows)),
        "inspections": sorted(observed, key=lambda item: item["row_id"]),
    }
    decisions = evaluate_quality_contracts(payload, threshold)
    payload["population_upper_bounds"] = decisions["population_upper_bounds"]
    payload["publication_decision"] = decisions["publication_decision"]
    payload["evidence_sha256"] = sha256_text(canonical_json(payload))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect a pre-deletion quality draw against verified lake snapshots "
        "and decide quality publication under the pre-registered contracts"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--draw", required=True)
    parser.add_argument(
        "--quality-threshold",
        default=str(DEFAULT_QUALITY_THRESHOLD),
        help="one-sided 95%% defect-rate gate as a fraction or decimal (default 1/4)",
    )
    args = parser.parse_args()
    try:
        threshold = Fraction(args.quality_threshold)
    except ValueError:
        parser.error(f"invalid --quality-threshold: {args.quality_threshold}")
    print(canonical_json(inspect_deletion_cohort(args.root, args.draw, threshold)))


if __name__ == "__main__":
    main()
