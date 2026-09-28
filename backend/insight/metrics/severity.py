"""Keep exact rational severity for notification thresholds; decimals are display values."""

from decimal import Decimal
from fractions import Fraction


def severity_fields(value):
    value = Fraction(value)
    return {
        "magnitude": str(Decimal(value.numerator) / Decimal(value.denominator)),
        "magnitude_ratio": [str(value.numerator), str(value.denominator)],
    }


def signal_severity(signal):
    ratio = signal.get("magnitude_ratio")
    return Fraction(int(ratio[0]), int(ratio[1])) if ratio else Fraction(Decimal(signal["magnitude"]))


def notification_baseline(event):
    ratio = event.notified_magnitude_ratio
    return Fraction(int(ratio[0]), int(ratio[1])) if ratio else Fraction(event.notified_magnitude)
