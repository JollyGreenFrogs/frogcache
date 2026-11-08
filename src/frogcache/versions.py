"""Version parsing and comparison (numeric dotted versions like 3.8.0).

Governing ADR: docs/decisions/ADR-001.
"""

import re


def parse_version(version: str) -> tuple[int, ...]:
    """Parse the leading dotted-numeric part of a version into a tuple of ints.

    Pre-release suffixes are ignored: '3.10.0a1' -> (3, 10, 0).

    Args:
        version: e.g. '3.8.0' -> (3, 8, 0); '3.10.0a1' -> (3, 10, 0).
    Returns:
        Tuple of the numeric components, in order.
    """
    match = re.match(r"[0-9]+(?:\.[0-9]+)*", version)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(candidate: str, reference: str) -> bool:
    """Return True if version candidate is strictly newer than reference.

    Tuples are zero-padded to equal length first, so '3.8' == '3.8.0'.
    Adequate for the x.y.z versions these artifacts use.

    Args:
        candidate: version to test.
        reference: version to compare against.
    Returns:
        True when candidate > reference.
    """
    left, right = parse_version(candidate), parse_version(reference)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right
