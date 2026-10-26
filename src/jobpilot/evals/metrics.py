"""Small, dependency-free ranking metrics used by the eval harness."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def precision_at_k(ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int) -> float:
    """Fraction of the first ``k`` results that are relevant."""
    if k <= 0:
        return 0.0
    relevant = set(relevant_ids)
    hits = sum(1 for item in ranked_ids[:k] if item in relevant)
    return hits / k


def ndcg_at_k(ranked_ids: Sequence[str], relevance: dict[str, int], k: int) -> float:
    """Normalised discounted cumulative gain with graded relevance."""
    if k <= 0 or not relevance:
        return 0.0

    def dcg(ids: Sequence[str]) -> float:
        total = 0.0
        for index, item in enumerate(ids[:k], start=1):
            gain = (2 ** relevance.get(item, 0)) - 1
            total += gain / math.log2(index + 1)
        return total

    ideal = sorted(relevance.values(), reverse=True)
    ideal_gain = sum(
        ((2**gain) - 1) / math.log2(index + 1) for index, gain in enumerate(ideal[:k], start=1)
    )
    return dcg(ranked_ids) / ideal_gain if ideal_gain else 0.0


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks for ties, enough for Spearman without scipy."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float:
    """Spearman rank correlation, returning 0 when either side is constant."""
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_ranks = _ranks(left)
    right_ranks = _ranks(right)
    mean_left = sum(left_ranks) / len(left_ranks)
    mean_right = sum(right_ranks) / len(right_ranks)
    numerator = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left_ranks, right_ranks, strict=True)
    )
    denominator = math.sqrt(
        sum((a - mean_left) ** 2 for a in left_ranks)
        * sum((b - mean_right) ** 2 for b in right_ranks)
    )
    return numerator / denominator if denominator else 0.0


def mean_absolute_error(pairs: Iterable[tuple[float, float]]) -> float:
    values = [abs(expected - actual) for expected, actual in pairs]
    return sum(values) / len(values) if values else 0.0


def sequence_accuracy(
    predicted: Sequence[Sequence[str]], expected: Sequence[Sequence[str]]
) -> float:
    if not expected:
        return 0.0
    hits = sum(
        1 for left, right in zip(predicted, expected, strict=True) if list(left) == list(right)
    )
    return hits / len(expected)
