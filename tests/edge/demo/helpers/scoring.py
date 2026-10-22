"""Scorecard points, in a separate module to test imports."""

AGE_BINS = [(25, -18), (35, 4), (50, 12), (200, 20)]


def points(age):
    """Points for an age."""
    for upper, pts in AGE_BINS:
        if age < upper:
            return pts
    return 0
