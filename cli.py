from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys

import yaml

from sideral_multimodels import __version__
from sideral_multimodels.core import MODEL_IDS, parse_hours, resolve_run
from sideral_multimodels.providers.downloaders import download_all
from sideral_multimodels.processing import process_run


def _config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {config_path}")
    with config_path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError("A raiz de config.yml deve ser um objeto YAML")
    return data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sideral-multimodels",
        description="SIDERAL MULTIMODELS 0.5 — download, regrade e consenso de três modelos.",
    )
    parser.add_argument("--version", action="version", version=f"SIDERAL MULTIMODELS {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Verifica dependências e configuração sem baixar dados")
    doctor.add_argument("--config", default="config.yml")
    for command in ("download", "process", "run"):
        cmd = sub.add_parser(command, help={"download": "Baixa campos GRIB2 dos três modelos", "process": "Regride e combina GRIB2 já baixados", "run": "Executa download e processamento"}[command])
        cmd.add_argument("--config", default="config.yml")
        cmd.add_argument("--date", default=None, help="Data da rodada UTC em YYYYMMDD; em branco = seleção automática com margem de disponibilidade")
        cmd.add_argument("--cycle", default=None, help="Ciclo UTC: 00, 06, 12 ou 18")
        cmd.add_argument("--hours", default=None, help="Horas separadas por vírgula; padrão é 0,6,...,168")
        cmd.add_argument("--raw-dir", default="data/raw")
        cmd.add_argument("--output-dir", default="output")
    return parser


def _log_setup() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _doctor(config: dict) -> int:
    print(f"SIDERAL MULTIMODELS {__version__}")
    print("Modelos configurados: " + ", ".join(config.get("forecast", {}).get("models", list(MODEL_IDS))))
    print("Grade: %.2f°" % float(config.get("grid", {}).get("resolution_degrees", 0.5)))
    print("Python: " + sys.version.split()[0])
    missing = []
    modules = ("numpy", "scipy", "xarray", "cfgrib", "eccodes", "requests", "yaml", "ecmwf.opendata", "netCDF4", "matplotlib")
    import importlib.util
    for module in modules:
        root = module.split(".")[0]
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module)
    if missing:
        print("Dependências ausentes: " + ", ".join(missing))
        return 1
    print("Dependências locais: OK")
    return 0


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(".env"))
    except ImportError:
        pass
    _log_setup()
    args = _parser().parse_args()
    try:
        config = _config(getattr(args, "config", "config.yml"))
        if args.command == "doctor":
            raise SystemExit(_doctor(config))
        fc = config.get("forecast", {})
        grid = config.get("grid", {})
        models = list(fc.get("models", MODEL_IDS))
        date, cycle = resolve_run(args.date, args.cycle, int(fc.get("automatic_cycle_delay_hours", 12)))
        hours = parse_hours(args.hours, int(fc.get("max_hour", 168)), int(fc.get("interval_hours", 6)))
        if args.command in ("download", "run"):
            download_all(date, cycle, hours, args.raw_dir, models)
        if args.command in ("process", "run"):
            modes = {model: info.get("precipitation_default", "cumulative") for model, info in config.get("models", {}).items()}
            result = process_run(
                date=date,
                cycle=cycle,
                hours=hours,
                raw_root=args.raw_dir,
                output_dir=Path(args.output_dir) / f"{date}{cycle}",
                resolution=float(grid.get("resolution_degrees", 0.5)),
                minimum_models=int(fc.get("minimum_models_per_cell", 2)),
                models=models,
                precipitation_modes=modes,
            )
            print(f"Produto criado: {result}")
            print(f"Manifesto: {result.parent / 'manifest.json'}")
    except SystemExit:
        raise
    except Exception as exc:
        logging.exception("Falha operacional")
        print(f"ERRO: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
