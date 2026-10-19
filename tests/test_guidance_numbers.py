"""Reading guided figures, on wordings taken from the releases in the universe."""

from decimal import Decimal

import pytest

from talk_is_cheap.guidance.numbers import ParsedValue, UnparsedValue, parse_value

B = Decimal(10) ** 9
M = Decimal(10) ** 6


def figures(value: ParsedValue) -> tuple[Decimal | None, Decimal | None, Decimal | None, str]:
    return value.low, value.high, value.point, value.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Nvidia: an amount plus or minus a percentage is relative.
        ("$108.0 billion, plus or minus 2%", (Decimal("105.84") * B, Decimal("110.16") * B,
                                              Decimal(108) * B, "usd")),
        # AMD: plus or minus an amount.
        ("approximately $13 billion, plus or minus $300 million",
         (Decimal("12.7") * B, Decimal("13.3") * B, 13 * B, "usd")),
        # Basis points around a percentage.
        ("74.0%, plus or minus 50 basis points",
         (Decimal("73.5"), Decimal("74.5"), Decimal("74.0"), "pct")),
        # A percentage plus or minus a percentage is plus or minus points (Nvidia tax rate).
        ("17.0%, plus or minus 1%", (Decimal(16), Decimal(18), Decimal(17), "pct")),
        ("75.0% \u00b1 50 bps", (Decimal("74.5"), Decimal("75.5"), Decimal(75), "pct")),
        ("$2.0 billion +/- $100 million", (Decimal("1.9") * B, Decimal("2.1") * B, 2 * B, "usd")),
    ],
)  # fmt: skip
def test_plus_or_minus(text: str, expected: tuple[object, ...]) -> None:
    assert figures(parse_value(text)) == expected


def test_plus_or_minus_is_kept_with_its_kind() -> None:
    relative = parse_value("$108.0 billion, plus or minus 2%")
    assert (relative.plus_minus, relative.plus_minus_kind) == (Decimal(2), "percent")
    points = parse_value("74.0%, plus or minus 50 basis points")
    assert (points.plus_minus, points.plus_minus_kind) == (Decimal("0.5"), "absolute")
    amount = parse_value("$13 billion, plus or minus $300 million")
    assert (amount.plus_minus, amount.plus_minus_kind) == (300 * M, "absolute")


@pytest.mark.parametrize(
    "text",
    [
        "$15.8-16.8 billion",  # Intel: hyphen, the scale written once
        "$15.8\u201316.8 billion",  # en dash
        "$15.8 \u2014 $16.8 billion",  # em dash with spaces
        "$15.8 billion to $16.8 billion",
        "between $15.8 billion and $16.8 billion",
        "from $15.8 billion to $16.8 billion",
        "$15.8B - $16.8B",  # Qualcomm
    ],
)
def test_ranges_in_any_dash_style(text: str) -> None:
    assert figures(parse_value(text)) == (Decimal("15.8") * B, Decimal("16.8") * B, None, "usd")


@pytest.mark.parametrize(
    ("text", "low", "high", "unit"),
    [
        ("$61-64 billion", 61 * B, 64 * B, "usd"),  # Meta
        ("$11.42 - $11.5 billion", Decimal("11.42") * B, Decimal("11.5") * B, "usd"),  # CRM
        ("65% - 66%", Decimal(65), Decimal(66), "pct"),  # Cisco
        ("12%-14%", Decimal(12), Decimal(14), "pct"),
        ("between 15-17%", Decimal(15), Decimal(17), "pct"),
        ("$1.15B - $1.35B", Decimal("1.15") * B, Decimal("1.35") * B, "usd"),
    ],
)
def test_ranges(text: str, low: Decimal, high: Decimal, unit: str) -> None:
    assert figures(parse_value(text)) == (low, high, None, unit)


def test_per_share_figures_and_negatives() -> None:
    assert figures(parse_value("$1.32 - $1.34", per_share=True)) == (
        Decimal("1.32"),
        Decimal("1.34"),
        None,
        "usd_per_share",
    )
    # Intel writes losses and negative tax rates in parentheses.
    assert parse_value("$(0.21)", per_share=True).point == Decimal("-0.21")
    assert parse_value("(79)%").point == Decimal(-79)


@pytest.mark.parametrize(
    ("text", "low", "high", "point"),
    [
        ("approximately $9.2 billion", None, None, Decimal("9.2") * B),
        ("~44.0%", None, None, Decimal("44.0")),
        ("at least $90 billion", 90 * B, None, None),  # Oracle
        ("20%+", Decimal(20), None, None),  # Netflix operating income growth
        ("more than 50%", Decimal(50), None, None),
        ("up to $2 billion", None, 2 * B, None),
        ("12% year over year", None, None, Decimal(12)),  # "over" after the figure
        ("a sequential decline of approximately 5%", None, None, Decimal(-5)),
        ("flat to up 2%", Decimal(0), Decimal(2), None),
    ],
)
def test_points_and_bounds(
    text: str, low: Decimal | None, high: Decimal | None, point: Decimal | None
) -> None:
    value = parse_value(text)
    assert (value.low, value.high, value.point) == (low, high, point)


def test_other_units() -> None:
    assert figures(parse_value("~389 million")) == (None, None, 389 * M, "other")


@pytest.mark.parametrize(
    "text",
    [
        "in the mid-70% range",  # Nvidia full-year gross margin
        "high-30% range",
        "low double-digit growth",
        "slightly above 12%",  # Salesforce
        "mid- to upper-40% range",
        "$1.83 and $1.91",  # two figures, not a range
        "74.9% and 75.0%, respectively",
        "notably larger",
    ],
)
def test_descriptions_are_not_figures(text: str) -> None:
    with pytest.raises(UnparsedValue):
        parse_value(text)
