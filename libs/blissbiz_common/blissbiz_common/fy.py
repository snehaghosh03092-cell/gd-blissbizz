"""Indian dates and financial years (GST documents are numbered per financial year)."""

from datetime import date, datetime, timedelta

IST = timedelta(hours=5, minutes=30)


def india_date(moment_utc: datetime) -> date:
    """The calendar date in India for a naive-UTC timestamp."""
    return (moment_utc + IST).date()


def financial_year(day: date, start_month: int = 4) -> str:
    """'2026-27' for any date from 1 April 2026 to 31 March 2027 (start month configurable)."""
    first = day.year if day.month >= start_month else day.year - 1
    return f"{first}-{(first + 1) % 100:02d}"


def document_number(series: str, fy: str, number: int) -> str:
    """'TC/26-27/00042': series, short financial year, five-digit gapless number."""
    return f"{series}/{fy[2:4]}-{fy[-2:]}/{number:05d}"
