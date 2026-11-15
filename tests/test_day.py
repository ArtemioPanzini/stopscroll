from datetime import datetime, timezone
import unittest
from stopscroll.day import day_key, next_reset


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class DayTests(unittest.TestCase):
    def test_before_reset_is_previous_day(self):
        self.assertEqual(day_key(stamp('2026-11-16T03:59'), 4, timezone.utc), '2026-11-15')

    def test_at_reset_is_new_day(self):
        self.assertEqual(day_key(stamp('2026-11-16T04:00'), 4, timezone.utc), '2026-11-16')

    def test_next_reset(self):
        self.assertEqual(next_reset(stamp('2026-11-15T12:00'), 4, timezone.utc), stamp('2026-11-16T04:00'))

    def test_next_reset_at_boundary_and_year_end(self):
        self.assertEqual(next_reset(stamp('2026-12-31T04:00'), 4, timezone.utc), stamp('2027-01-01T04:00'))

    def test_leap_day(self):
        self.assertEqual(day_key(stamp('2028-03-01T03:59'), 4, timezone.utc), '2028-02-29')
