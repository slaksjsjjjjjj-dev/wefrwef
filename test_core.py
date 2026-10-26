import numpy as np
import pytest

from sideral_multimodels.core import MODEL_IDS, combine_arrays, make_target_grid, parse_hours, resolve_run


def test_parse_hours_default_includes_zero_and_max():
    hours = parse_hours(None)
    assert hours[0] == 0
    assert hours[-1] == 168
    assert len(hours) == 29


def test_parse_hours_rejects_missing_zero_and_duplicates():
    with pytest.raises(ValueError):
        parse_hours("6,12")
    with pytest.raises(ValueError):
        parse_hours("0,6,6")
    with pytest.raises(ValueError):
        parse_hours("0,5")


def test_target_grid_is_global_half_degree():
    lat, lon = make_target_grid(0.5)
    assert len(lat) == 361
    assert len(lon) == 720
    assert lat[0] == -90 and lat[-1] == 90
    assert lon[0] == -180 and lon[-1] == 179.5


def test_combine_arrays_tracks_count_spread_and_agreement():
    a = np.array([[10.0, np.nan], [4.0, 3.0]], dtype=np.float32)
    b = np.array([[12.0, 2.0], [4.0, np.nan]], dtype=np.float32)
    c = np.array([[11.0, 4.0], [np.nan, np.nan]], dtype=np.float32)
    result = combine_arrays([a, b, c], minimum_models=2, agreement_scale=4)
    assert result["mean"][0, 0] == pytest.approx(11.0)
    assert result["nmodels"][0, 0] == 3
    assert result["mean"][0, 1] == pytest.approx(3.0)
    assert result["nmodels"][1, 0] == 2
    assert np.isnan(result["mean"][1, 1])
    assert np.isnan(result["agreement"][1, 1])
    assert 0 <= result["agreement"][0, 0] <= 100


def test_resolve_run_checks_explicit_cycle():
    assert resolve_run("20261026", "06") == ("20261026", "06")
    with pytest.raises(ValueError):
        resolve_run("20261026", "09")
    with pytest.raises(ValueError):
        resolve_run("20261026", None)


def test_multimodels_uses_only_ecmwf_gfs_and_icon():
    assert MODEL_IDS == ("ecmwf", "gfs", "icon")
