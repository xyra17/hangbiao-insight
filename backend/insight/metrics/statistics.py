"""Binomial intervals only; never apply these to units per session."""

from math import sqrt


def validate_counts(successes, total):
    if type(successes) is not int or type(total) is not int or not 0 <= successes <= total:
        raise ValueError("Invalid binomial counts")


def wilson(successes, total):
    validate_counts(successes, total)
    if total == 0:
        return None
    z = 1.959963984540054
    p = successes / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0.0, center - radius), min(1.0, center + radius)]


def newcombe(current_successes, current_total, baseline_successes, baseline_total):
    validate_counts(current_successes, current_total)
    validate_counts(baseline_successes, baseline_total)
    if not current_total or not baseline_total:
        return None
    p, q = current_successes / current_total, baseline_successes / baseline_total
    lo_p, hi_p = wilson(current_successes, current_total)
    lo_q, hi_q = wilson(baseline_successes, baseline_total)
    return [p - q - sqrt((p - lo_p) ** 2 + (hi_q - q) ** 2), p - q + sqrt((hi_p - p) ** 2 + (q - lo_q) ** 2)]
