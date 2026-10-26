from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from scipy.interpolate import RegularGridInterpolator
from scipy.spatial import cKDTree

from sideral_multimodels.core import (
    AGREEMENT_SCALES, FIELD_NAMES, FIELD_UNITS, MODEL_IDS, combine_arrays,
    make_target_grid, raw_path,
)

LOG = logging.getLogger("sideral_multimodels.process")

ALIASES = {
    "t2m": {"t2m", "2t", "tmp_2m", "temperature_2m"},
    "d2m": {"d2m", "2d", "td_2m", "dewpoint_2m"},
    "u10": {"u10", "10u", "u_10m", "u-component_of_wind_10m"},
    "v10": {"v10", "10v", "v_10m", "v-component_of_wind_10m"},
    "msl": {"msl", "prmsl", "mslet", "pmsl", "mean_sea_level_pressure"},
    "tp": {"tp", "apcp", "tot_prec", "total_precipitation", "precipitation"},
}
FIELD_ORDER = tuple(ALIASES.keys())


def _key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _canonical_field(name: str, attrs: dict[str, Any]) -> str | None:
    candidates = [_key(name), _key(attrs.get("GRIB_shortName")), _key(attrs.get("shortName"))]
    for canonical, aliases in ALIASES.items():
        if any(candidate in aliases for candidate in candidates):
            return canonical
    return None

def _coord(da: xr.DataArray, names: tuple[str, ...]) -> np.ndarray | None:
    for name in names:
        if name in da.coords:
            try:
                return np.asarray(da.coords[name].values, dtype=np.float64)
            except (TypeError, ValueError):
                continue
    return None


def _step_value(attrs: dict[str, Any], key: str) -> float | None:
    value = attrs.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _precip_steps(attrs: dict[str, Any], lead: int) -> tuple[float | None, float | None]:
    start = _step_value(attrs, "GRIB_startStep")
    end = _step_value(attrs, "GRIB_endStep")
    raw = attrs.get("GRIB_stepRange") or attrs.get("stepRange")
    if (start is None or end is None) and raw is not None:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-:]\s*(\d+(?:\.\d+)?)\s*", str(raw))
        if match:
            start, end = float(match.group(1)), float(match.group(2))
    if end is None:
        end = float(lead)
    return start, end


def _to_canonical_units(values: np.ndarray, field: str, units: str | None) -> np.ndarray:
    result = np.array(values, dtype=np.float32, copy=True)
    unit = (units or "").strip().lower().replace(" ", "")
    if field in {"t2m", "d2m"}:
        if unit in {"k", "kelvin", "degrees_k", "degk"} or "kelvin" in unit:
            result = result - np.float32(273.15)
    elif field == "msl":
        if unit in {"pa", "pascal", "pascals", "n/m2", "nm-2"} or unit.startswith("pa{"):
            result = result / np.float32(100.0)
        elif unit in {"hpa", "mbar", "millibar"}:
            pass
    elif field == "tp":
        # 1 kg m-2 de água líquida equivale a 1 mm de precipitação.
        if unit in {"m", "metre", "meter", "mofwater"}:
            result = result * np.float32(1000.0)
    result[~np.isfinite(result)] = np.nan
    return result


def _sphere(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    lat_r = np.deg2rad(lat.ravel())
    lon_r = np.deg2rad(lon.ravel())
    return np.column_stack((np.cos(lat_r) * np.cos(lon_r), np.cos(lat_r) * np.sin(lon_r), np.sin(lat_r)))


class Regridder:
    """Regrade para 0,5°. Grade regular: interpolação linear; grade irregular: vizinho mais próximo."""
    def __init__(self, resolution: float = 0.5):
        self.target_lat, self.target_lon = make_target_grid(resolution)
        self.lon_mesh, self.lat_mesh = np.meshgrid(self.target_lon, self.target_lat)
        self.target_points = _sphere(self.lat_mesh, self.lon_mesh)
        self._tree_indices: dict[str, np.ndarray] = {}
        self._tree_valid: dict[str, np.ndarray] = {}

    def apply(self, values: np.ndarray, latitude: np.ndarray, longitude: np.ndarray) -> np.ndarray:
        data = np.asarray(values, dtype=np.float32)
        lat = np.asarray(latitude, dtype=np.float64)
        lon = np.asarray(longitude, dtype=np.float64)
        data = np.squeeze(data)

        # Grade regular latitude x longitude.
        if lat.ndim == 1 and lon.ndim == 1 and lat.size != lon.size and data.ndim == 2:
            if data.shape == (lon.size, lat.size):
                data = data.T
            if data.shape != (lat.size, lon.size):
                raise ValueError(f"Campo regular incompatível com coordenadas: {data.shape}, lat={lat.size}, lon={lon.size}")
            lat_norm = lat.copy()
            lat_order = np.argsort(lat_norm)
            lat_norm = lat_norm[lat_order]
            data = data[lat_order, :]
            lon_norm = ((lon + 180.0) % 360.0) - 180.0
            lon_order = np.argsort(lon_norm)
            lon_norm = lon_norm[lon_order]
            data = data[:, lon_order]
            # Remove coordenadas repetidas, como -180 e 180 no mesmo arquivo.
            keep = np.ones(lon_norm.size, dtype=bool)
            keep[1:] = np.diff(lon_norm) > 1e-7
            lon_norm = lon_norm[keep]
            data = data[:, keep]
            if lon_norm.size >= 3 and lon_norm[-1] - lon_norm[0] > 300:
                lon_ext = np.concatenate(([lon_norm[-1] - 360.0], lon_norm, [lon_norm[0] + 360.0]))
                data_ext = np.concatenate((data[:, [-1]], data, data[:, [0]]), axis=1)
            else:
                lon_ext, data_ext = lon_norm, data
            interp = RegularGridInterpolator(
                (lat_norm, lon_ext), data_ext, method="linear", bounds_error=False, fill_value=np.nan
            )
            query = np.column_stack((self.lat_mesh.ravel(), self.lon_mesh.ravel()))
            return interp(query).reshape(self.lat_mesh.shape).astype(np.float32)

        # Grade irregular (por exemplo, a grade triangular/icosaédrica do ICON).
        if lat.shape == lon.shape and data.size == lat.size and lat.size > 100:
            signature = hashlib.sha1(lat.astype("float32").tobytes() + lon.astype("float32").tobytes()).hexdigest()
            indices = self._tree_indices.get(signature)
            if indices is None:
                tree = cKDTree(_sphere(lat, lon))
                distance, indices = tree.query(self.target_points, k=1, workers=-1)
                # Não extrapolar uma grade regional para o globo todo. 120 km deixa
                # margem para a grade global nativa e para a grade de destino de 0,5°.
                valid = distance <= (2.0 * np.sin((120.0 / 6371.0) / 2.0))
                indices = indices.astype(np.int64)
                indices[~valid] = -1
                self._tree_indices[signature] = indices
                self._tree_valid[signature] = valid
            valid = self._tree_valid[signature]
            flat = np.full(self.target_points.shape[0], np.nan, dtype=np.float32)
            flat[valid] = data.ravel()[indices[valid]]
            return flat.reshape(self.lat_mesh.shape)

        # Alguns arquivos apresentam uma malha 2D em coordenadas auxiliares.
        if lat.shape == lon.shape and lat.shape == data.shape and data.ndim == 2:
            signature = hashlib.sha1(lat.astype("float32").tobytes() + lon.astype("float32").tobytes()).hexdigest()
            indices = self._tree_indices.get(signature)
            if indices is None:
                tree = cKDTree(_sphere(lat, lon))
                distance, indices = tree.query(self.target_points, k=1, workers=-1)
                # Não extrapolar uma grade regional para o globo todo. 120 km deixa
                # margem para a grade global nativa e para a grade de destino de 0,5°.
                valid = distance <= (2.0 * np.sin((120.0 / 6371.0) / 2.0))
                indices = indices.astype(np.int64)
                indices[~valid] = -1
                self._tree_indices[signature] = indices
                self._tree_valid[signature] = valid
            valid = self._tree_valid[signature]
            flat = np.full(self.target_points.shape[0], np.nan, dtype=np.float32)
            flat[valid] = data.ravel()[indices[valid]]
            return flat.reshape(self.lat_mesh.shape)

        raise ValueError(f"Formato de grade GRIB não suportado: valores={data.shape}, lat={lat.shape}, lon={lon.shape}")


def _read_fields(path: Path, lead: int, regridder: Regridder) -> dict[str, dict[str, Any]]:
    try:
        datasets = xr.backends.list_engines()
        if "cfgrib" not in datasets:
            raise RuntimeError("engine cfgrib não está instalado")
        import cfgrib
        opened = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    except Exception as exc:
        raise RuntimeError(f"Não foi possível ler {path.name} como GRIB2: {exc}") from exc

    result: dict[str, dict[str, Any]] = {}
    try:
        for ds in opened:
            for name, da in ds.data_vars.items():
                attrs = dict(da.attrs)
                field = _canonical_field(name, attrs)
                if field is None or field in result:
                    continue
                lat = _coord(da, ("latitude", "lat"))
                lon = _coord(da, ("longitude", "lon"))
                if lat is None or lon is None:
                    continue
                values = np.asarray(da.values)
                values = np.squeeze(values)
                # Para malhas regulares com coordenadas como dimensões, garante que o eixo
                # vertical da matriz corresponde à latitude e o horizontal à longitude.
                lat_dim = da.coords["latitude"].dims[0] if "latitude" in da.coords and da.coords["latitude"].ndim == 1 else None
                lon_dim = da.coords["longitude"].dims[0] if "longitude" in da.coords and da.coords["longitude"].ndim == 1 else None
                if values.ndim == 2 and lat_dim and lon_dim and lat_dim != lon_dim and set((lat_dim, lon_dim)).issubset(da.dims):
                    values = np.asarray(da.transpose(lat_dim, lon_dim).values)
                else:
                    values = np.squeeze(values)
                canonical_unit_field = "tp" if field == "tp" else field
                values = _to_canonical_units(values, canonical_unit_field, attrs.get("units"))
                gridded = regridder.apply(values, lat, lon)
                start, end = _precip_steps(attrs, lead) if field == "tp" else (None, None)
                result[field] = {
                    "values": gridded,
                    "units": FIELD_UNITS.get("tp6h") if field == "tp" else FIELD_UNITS.get(field),
                    "start_step": start,
                    "end_step": end,
                    "raw_units": attrs.get("units", "unknown"),
                    "source_name": name,
                    "step_type": attrs.get("GRIB_stepType", attrs.get("stepType", "unknown")),
                }
        if not result:
            raise RuntimeError(f"Nenhuma variável reconhecida em {path.name}; verifique os shortName/níveis do GRIB")
        return result
    finally:
        for ds in opened:
            try:
                ds.close()
            except Exception:
                pass


def _precipitation_intervals(
    model: str,
    hours: list[int],
    fields_by_hour: dict[int, dict[str, dict[str, Any]]],
    default_mode: str = "cumulative",
    grid_shape: tuple[int, int] = (361, 720),
) -> dict[int, np.ndarray]:
    result: dict[int, np.ndarray] = {}
    previous_raw: np.ndarray | None = None
    previous_lead: int | None = None
    for lead in hours:
        entry = fields_by_hour.get(lead, {}).get("tp")
        if not entry:
            result[lead] = np.full(grid_shape, np.nan, dtype=np.float32)
            continue
        current = entry["values"].astype(np.float32)
        start = entry.get("start_step")
        end = entry.get("end_step")
        if lead == 0:
            result[lead] = np.zeros_like(current)
            previous_raw, previous_lead = current, lead
            continue

        mode = default_mode
        if start is not None and end is not None:
            if abs(float(start)) < 1e-5 and abs(float(end) - float(lead)) < 1e-5:
                mode = "cumulative"
            elif previous_lead is not None and abs(float(start) - float(previous_lead)) < 1e-5 and abs(float(end) - float(lead)) < 1e-5:
                mode = "interval"
            else:
                LOG.warning("%s F%03d: stepRange %s–%s não é um intervalo de 6 h claramente identificado", model.upper(), lead, start, end)
        if mode == "cumulative":
            if previous_raw is not None and previous_lead is not None and lead - previous_lead == 6:
                interval = current - previous_raw
                interval[interval < 0] = 0.0
                result[lead] = interval
            else:
                result[lead] = np.full_like(current, np.nan)
        elif mode == "interval":
            result[lead] = current
        else:
            result[lead] = np.full_like(current, np.nan)
        previous_raw, previous_lead = current, lead
    return result


def process_run(
    date: str,
    cycle: str,
    hours: list[int],
    raw_root: str | Path,
    output_dir: str | Path,
    resolution: float = 0.5,
    minimum_models: int = 2,
    models: list[str] | None = None,
    precipitation_modes: dict[str, str] | None = None,
) -> Path:
    raw_root = Path(raw_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_models = models or list(MODEL_IDS)
    regridder = Regridder(resolution)
    all_model_fields: dict[str, dict[int, dict[str, dict[str, Any]]]] = {}

    for model in selected_models:
        model_data: dict[int, dict[str, dict[str, Any]]] = {}
        LOG.info("Processando campos do modelo %s", model.upper())
        for lead in hours:
            path = raw_path(raw_root, model, date, cycle, lead)
            if not path.exists() or path.stat().st_size < 100:
                LOG.warning("Arquivo ausente para %s F%03d: %s", model.upper(), lead, path)
                continue
            model_data[lead] = _read_fields(path, lead, regridder)
        if not model_data or not any("t2m" in item for item in model_data.values()):
            raise RuntimeError(f"Nenhum campo de temperatura a 2 m reconhecido para {model.upper()}; o produto exige os modelos selecionados")
        all_model_fields[model] = model_data

    target_lat, target_lon = make_target_grid(resolution)
    output_data: dict[str, list[np.ndarray]] = {field: [] for field in FIELD_NAMES}
    qc: dict[str, dict[str, Any]] = {field: {"hours": {}, "available_models": []} for field in FIELD_NAMES}
    normalized_by_model: dict[str, dict[int, dict[str, np.ndarray]]] = {}

    for model, per_hour in all_model_fields.items():
        normalized: dict[int, dict[str, np.ndarray]] = {hour: {} for hour in hours}
        mode = (precipitation_modes or {}).get(model, "cumulative")
        precip_by_hour = _precipitation_intervals(model, hours, per_hour, mode, (regridder.target_lat.size, regridder.target_lon.size))
        for lead in hours:
            fields = per_hour.get(lead, {})
            for field in ("t2m", "d2m", "u10", "v10", "msl"):
                if field in fields:
                    normalized[lead][field] = fields[field]["values"]
            if lead in precip_by_hour and "tp" in per_hour.get(lead, {}):
                normalized[lead]["tp6h"] = precip_by_hour[lead]
        normalized_by_model[model] = normalized

    # O consenso é calculado ponto a ponto usando somente modelos com dados finitos.
    diagnostics: dict[str, dict[str, list[np.ndarray]]] = {}
    for lead in hours:
        for field in FIELD_NAMES:
            arrays = [normalized_by_model[m][lead][field] for m in selected_models if field in normalized_by_model[m].get(lead, {})]
            if not arrays:
                combined = {
                    "mean": np.full((target_lat.size, target_lon.size), np.nan, dtype=np.float32),
                    "spread": np.full((target_lat.size, target_lon.size), np.nan, dtype=np.float32),
                    "nmodels": np.zeros((target_lat.size, target_lon.size), dtype=np.uint8),
                    "agreement": np.full((target_lat.size, target_lon.size), np.nan, dtype=np.float32),
                }
            else:
                combined = combine_arrays(arrays, minimum_models, AGREEMENT_SCALES[field])
            output_data[field].append(combined["mean"])
            finite_counts = [int(np.isfinite(array).sum()) for array in arrays]
            qc[field]["hours"][str(lead)] = {
                "contributing_models": len(arrays),
                "finite_cells_per_source": finite_counts,
                "cells_with_minimum_models": int(np.isfinite(combined["mean"]).sum()),
            }
            for model in selected_models:
                if field in normalized_by_model[model].get(lead, {}) and model not in qc[field]["available_models"]:
                    qc[field]["available_models"].append(model)
            # Preserve diagnostic arrays in a separate collection on the Dataset.
            diagnostics.setdefault(field, {"spread": [], "nmodels": [], "agreement": []})
            for key in ("spread", "nmodels", "agreement"):
                diagnostics[field][key].append(combined[key])

    coords = {"forecast_hour": np.asarray(hours, dtype=np.int16), "latitude": target_lat, "longitude": target_lon}
    data_vars: dict[str, Any] = {}
    for field in FIELD_NAMES:
        attrs_units = FIELD_UNITS[field]
        data_vars[field] = (("forecast_hour", "latitude", "longitude"), np.stack(output_data[field]).astype(np.float32), {"units": attrs_units, "long_name": field})
        for key, human in (("spread", "desvio padrão entre modelos"), ("nmodels", "número de modelos com dados"), ("agreement", "índice de concordância não probabilístico")):
            stack = np.stack(diagnostics[field][key])
            if key == "nmodels":
                stack = stack.astype(np.uint8)
                units = "1"
            elif key == "agreement":
                stack = stack.astype(np.float32)
                units = "%"
            else:
                stack = stack.astype(np.float32)
                units = attrs_units
            data_vars[f"{field}_{key}"] = (("forecast_hour", "latitude", "longitude"), stack, {"units": units, "long_name": f"{field}: {human}"})

    from datetime import datetime, timezone, timedelta
    init = datetime.strptime(date + cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    valid_times = [(init + timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ") for lead in hours]
    dataset = xr.Dataset(
        data_vars=data_vars,
        coords={**coords, "valid_time": ("forecast_hour", np.asarray(valid_times, dtype="U20"))},
        attrs={
            "title": "SIDERAL MULTIMODELS 0.5",
            "version": "0.1.0",
            "run_initialization_utc": init.isoformat().replace("+00:00", "Z"),
            "grid_resolution_degrees": float(resolution),
            "models_configured": ",".join(selected_models),
            "minimum_models_per_cell": int(minimum_models),
            "method": "Média aritmética simples por célula, com máscara por número mínimo de fontes; spread é desvio padrão; agreement é índice descritivo exp(-spread/escala), não probabilidade calibrada.",
            "precipitation_note": "tp6h tenta converter acumulações nativas para intervalo de 6 h usando metadados stepRange; células sem suporte temporal comparável ficam NaN.",
        },
    )

    nc_path = output_dir / "consensus.nc"
    encoding: dict[str, dict[str, Any]] = {}
    for name in dataset.data_vars:
        if name.endswith("_nmodels"):
            encoding[name] = {"zlib": True, "complevel": 4, "dtype": "u1"}
        else:
            encoding[name] = {"zlib": True, "complevel": 4, "dtype": "f4", "_FillValue": np.float32(-9999.0)}
    dataset.to_netcdf(nc_path, engine="netcdf4", encoding=encoding)
    dataset.close()

    manifest = {
        "product": "SIDERAL MULTIMODELS 0.5",
        "version": "0.1.0",
        "status": "complete_with_qc",
        "runDate": date,
        "runCycle": f"{cycle}Z",
        "initTime": init.isoformat().replace("+00:00", "Z"),
        "resolutionDegrees": resolution,
        "grid": {"latitudes": int(target_lat.size), "longitudes": int(target_lon.size), "latitudeRange": [float(target_lat[0]), float(target_lat[-1])], "longitudeRange": [float(target_lon[0]), float(target_lon[-1])]},
        "forecastHours": hours,
        "models": selected_models,
        "minimumModelsPerCell": minimum_models,
        "variables": {field: {"units": FIELD_UNITS[field], "label": field, "agreementScale": AGREEMENT_SCALES[field], "sourceAvailability": qc[field]["available_models"]} for field in FIELD_NAMES},
        "qualityControl": qc,
        "outputs": ["consensus.nc", "manifest.json", "qc.json"],
        "methodNotes": [
            "Regrade linear para grades regulares; vizinho mais próximo para grades irregulares, incluindo ICON global.",
            "Média simples não é um ensemble probabilístico calibrado.",
            "Índice de concordância deriva do spread e não deve ser interpretado como probabilidade de ocorrência.",
            "Precipitação é normalizada para janela de 6 h somente quando o stepRange permite estimar a janela; dados sem intervalo comparável são marcados como ausentes.",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "qc.json").write_text(json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    _make_plots(dataset, output_dir / "plots")
    LOG.info("Produto escrito em %s", nc_path)
    return nc_path


def _make_plots(dataset: xr.Dataset, output_dir: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        LOG.warning("matplotlib indisponível; ignorando imagens de prévia")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    available = set(int(value) for value in dataset.forecast_hour.values)
    selected_hours = [hour for hour in (6, 24, 72, 120, 168) if hour in available]
    for lead in selected_hours:
        for field in ("t2m", "tp6h"):
            if field not in dataset or not np.isfinite(dataset[field].sel(forecast_hour=lead).values).any():
                continue
            values = dataset[field].sel(forecast_hour=lead).values
            fig, ax = plt.subplots(figsize=(12, 5.5), dpi=100)
            mesh = ax.imshow(values, origin="lower", extent=(-180, 180, -90, 90), aspect="auto", interpolation="nearest")
            ax.set_title(f"SIDERAL MULTIMODELS 0.5 — {field} — F{lead:03d}")
            ax.set_xlabel("Longitude")
            ax.set_ylabel("Latitude")
            fig.colorbar(mesh, ax=ax, shrink=0.82, label=FIELD_UNITS[field])
            fig.tight_layout()
            fig.savefig(output_dir / f"{field}_f{lead:03d}.png")
            plt.close(fig)
