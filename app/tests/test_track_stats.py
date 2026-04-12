"""Tests for track_stats module — written first (TDD RED phase)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from track_stats import (
    calculate_haversine_distance_m,
    calculate_track_distance_m,
    calculate_elevation_gain_m,
)


@dataclass
class FakePoint:
    latitude: float
    longitude: float
    elevation: float | None = None


# ---------------------------------------------------------------------------
# calculate_haversine_distance_m
# ---------------------------------------------------------------------------

class TestHaversineDistance:
    def test_same_point_is_zero(self) -> None:
        assert calculate_haversine_distance_m(48.0, 11.0, 48.0, 11.0) == pytest.approx(0.0)

    def test_known_distance_munich_to_berlin(self) -> None:
        # Munich ~(48.137, 11.576) → Berlin ~(52.520, 13.405) ≈ 504 km
        dist = calculate_haversine_distance_m(48.137, 11.576, 52.520, 13.405)
        assert 500_000 < dist < 510_000

    def test_short_distance_is_reasonable(self) -> None:
        # Move ~111 m north (1 arc-second of latitude ≈ 30.9 m; 0.001° ≈ 111 m)
        dist = calculate_haversine_distance_m(48.0, 11.0, 48.001, 11.0)
        assert 100 < dist < 130

    def test_result_is_float(self) -> None:
        result = calculate_haversine_distance_m(0.0, 0.0, 1.0, 0.0)
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# calculate_track_distance_m
# ---------------------------------------------------------------------------

class TestCalculateTrackDistanceM:
    def test_empty_points_returns_zero(self) -> None:
        assert calculate_track_distance_m([]) == pytest.approx(0.0)

    def test_single_point_returns_zero(self) -> None:
        pts = [FakePoint(48.0, 11.0)]
        assert calculate_track_distance_m(pts) == pytest.approx(0.0)

    def test_two_points_matches_haversine(self) -> None:
        pts = [FakePoint(48.0, 11.0), FakePoint(48.001, 11.0)]
        expected = calculate_haversine_distance_m(48.0, 11.0, 48.001, 11.0)
        assert calculate_track_distance_m(pts) == pytest.approx(expected)

    def test_three_points_sums_segments(self) -> None:
        pts = [
            FakePoint(48.0, 11.0),
            FakePoint(48.001, 11.0),
            FakePoint(48.002, 11.0),
        ]
        seg1 = calculate_haversine_distance_m(48.0, 11.0, 48.001, 11.0)
        seg2 = calculate_haversine_distance_m(48.001, 11.0, 48.002, 11.0)
        assert calculate_track_distance_m(pts) == pytest.approx(seg1 + seg2)

    def test_points_without_lat_lon_are_skipped(self) -> None:
        """Points with None latitude/longitude are silently skipped."""
        @dataclass
        class BadPoint:
            latitude: float | None = None
            longitude: float | None = None

        pts = [BadPoint(None, None), BadPoint(None, None)]
        assert calculate_track_distance_m(pts) == pytest.approx(0.0)

    def test_result_is_float(self) -> None:
        pts = [FakePoint(48.0, 11.0), FakePoint(49.0, 12.0)]
        assert isinstance(calculate_track_distance_m(pts), float)


# ---------------------------------------------------------------------------
# calculate_elevation_gain_m
# ---------------------------------------------------------------------------

class TestCalculateElevationGainM:
    def test_empty_points_returns_zero(self) -> None:
        assert calculate_elevation_gain_m([]) == pytest.approx(0.0)

    def test_single_point_returns_zero(self) -> None:
        pts = [FakePoint(0.0, 0.0, elevation=100.0)]
        assert calculate_elevation_gain_m(pts) == pytest.approx(0.0)

    def test_flat_track_returns_zero(self) -> None:
        pts = [FakePoint(0.0, 0.0, elevation=100.0) for _ in range(5)]
        assert calculate_elevation_gain_m(pts) == pytest.approx(0.0)

    def test_steady_climb_sums_gain(self) -> None:
        # 5 points ascending 10 m each = 40 m total gain (4 segments)
        pts = [FakePoint(float(i), 0.0, elevation=float(i * 10)) for i in range(5)]
        assert calculate_elevation_gain_m(pts) == pytest.approx(40.0)

    def test_descent_not_counted(self) -> None:
        # Go up 50 m then down 30 m → gain is only the 50 m up
        pts = [
            FakePoint(0.0, 0.0, elevation=0.0),
            FakePoint(1.0, 0.0, elevation=50.0),
            FakePoint(2.0, 0.0, elevation=20.0),
        ]
        assert calculate_elevation_gain_m(pts) == pytest.approx(50.0)

    def test_mixed_up_down_counts_only_positive(self) -> None:
        elevations = [0.0, 10.0, 5.0, 20.0, 15.0, 25.0]
        pts = [FakePoint(float(i), 0.0, elevation=e) for i, e in enumerate(elevations)]
        # Gains: +10, 0, +15, 0, +10 = 35
        assert calculate_elevation_gain_m(pts) == pytest.approx(35.0)

    def test_points_without_elevation_are_skipped(self) -> None:
        pts = [
            FakePoint(0.0, 0.0, elevation=None),
            FakePoint(1.0, 0.0, elevation=None),
        ]
        assert calculate_elevation_gain_m(pts) == pytest.approx(0.0)

    def test_partial_elevation_data_skips_gaps(self) -> None:
        """Consecutive pairs where both have elevation are counted."""
        pts = [
            FakePoint(0.0, 0.0, elevation=0.0),
            FakePoint(1.0, 0.0, elevation=None),   # gap — skip this pair
            FakePoint(2.0, 0.0, elevation=100.0),  # can't pair back through None
        ]
        # Only first→second pair is attempted; second has no elevation so nothing counted
        assert calculate_elevation_gain_m(pts) == pytest.approx(0.0)

    def test_result_is_float(self) -> None:
        pts = [FakePoint(0.0, 0.0, elevation=0.0), FakePoint(1.0, 0.0, elevation=10.0)]
        assert isinstance(calculate_elevation_gain_m(pts), float)
