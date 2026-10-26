import numpy as np
import pytest

from sideral_multimodels.processing import _precip_steps, _to_canonical_units


def test_precip_step_range_parsing():
    assert _precip_steps({"GRIB_stepRange": "6-12"}, 12) == (6.0, 12.0)
    assert _precip_steps({"GRIB_startStep": 0, "GRIB_endStep": 18}, 18) == (0.0, 18.0)


def test_temperature_and_pressure_units():
    temp = _to_canonical_units(np.array([273.15, 293.15]), "t2m", "K")
    np.testing.assert_allclose(temp, [0.0, 20.0], atol=1e-5)
    pressure = _to_canonical_units(np.array([100000.0]), "msl", "Pa")
    np.testing.assert_allclose(pressure, [1000.0])



def test_common_model_surface_field_aliases():
    from sideral_multimodels.processing import _canonical_field

    assert _canonical_field("2t", {}) == "t2m"
    assert _canonical_field("2d", {}) == "d2m"
    assert _canonical_field("10u", {}) == "u10"
    assert _canonical_field("10v", {}) == "v10"
    assert _canonical_field("prmsl", {}) == "msl"
    assert _canonical_field("unknown_field", {}) is None
