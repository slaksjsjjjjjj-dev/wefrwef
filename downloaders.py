from __future__ import annotations

import bz2
import gzip
import json
import logging
import os
import re
import shutil
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests

from sideral_multimodels.core import MODEL_IDS, raw_path

LOG = logging.getLogger("sideral_multimodels.download")
USER_AGENT = "Sideral-MultiModels-0.5/0.1 (meteorological research; contact: sideralmeteorologia@gmail.com)"
TIMEOUT = (20, 180)

# O DWD distribui estes parâmetros em diretórios separados. Cada campo é convertido
# para GRIB2 descomprimido e concatenado: GRIB2 permite múltiplas mensagens por arquivo.
ICON_FIELDS = {
    "t2m": ("t_2m", "T_2M"),
    "d2m": ("td_2m", "TD_2M"),
    "u10": ("u_10m", "U_10M"),
    "v10": ("v_10m", "V_10M"),
    "msl": ("pmsl", "PMSL"),
    "tp6h": ("tot_prec", "TOT_PREC"),
}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _get(session: requests.Session, url: str, *, params=None, headers=None, attempts=4) -> requests.Response:
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                wait = min(4 * (attempt + 1), 20)
                LOG.warning("Falha HTTP em %s (%s/%s): %s; nova tentativa em %ss", url, attempt + 1, attempts, exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"Não foi possível obter {url}: {last_error}")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_bytes(content)
    if temp.stat().st_size < 100:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"Arquivo de dados vazio ou pequeno demais: {path}")
    temp.replace(path)


def _decompress(content: bytes, url: str) -> bytes:
    low = url.lower()
    try:
        if low.endswith(".bz2") or content.startswith(b"BZh"):
            return bz2.decompress(content)
        if low.endswith(".gz") or content.startswith(b"\x1f\x8b"):
            return gzip.decompress(content)
        if low.endswith(".zip") or content.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(BytesIO(content)) as archive:
                members = [name for name in archive.namelist() if not name.endswith("/")]
                grib_members = [name for name in members if "grib" in name.lower() or name.lower().endswith((".grb", ".grb2"))]
                selected = grib_members or members
                if not selected:
                    raise RuntimeError("O ZIP não contém arquivos")
                return b"".join(archive.read(name) for name in selected)
    except (OSError, EOFError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"Falha ao descomprimir resposta de {url}: {exc}") from exc
    return content


def download_ecmwf(date: str, cycle: str, hours: list[int], root: Path) -> None:
    try:
        from ecmwf.opendata import Client
    except ImportError as exc:
        raise RuntimeError("Instale ecmwf-opendata: pip install ecmwf-opendata") from exc

    client = Client(source="ecmwf", model="ifs", resol="0p25")
    # No Open Data do ECMWF, as rodadas 06Z/18Z têm horizonte menor que 00Z/12Z.
    effective_max = 144 if cycle in {"06", "18"} else 168
    for lead in hours:
        if lead > effective_max:
            LOG.warning("ECMWF %sZ: F%03d fora do horizonte público esperado; pulando", cycle, lead)
            continue
        path = raw_path(root, "ecmwf", date, cycle, lead)
        if path.exists() and path.stat().st_size > 100:
            LOG.info("ECMWF F%03d já existe; mantendo cache", lead)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".grib2.part")
        try:
            LOG.info("ECMWF IFS: baixando rodada %s %sZ F%03d", date, cycle, lead)
            client.retrieve(
                date=date,
                time=int(cycle),
                stream="oper",
                type="fc",
                step=lead,
                param=["2t", "2d", "10u", "10v", "msl", "tp"],
                target=str(temp),
            )
            if not temp.exists() or temp.stat().st_size < 100:
                raise RuntimeError("ECMWF não retornou um GRIB2 válido")
            temp.replace(path)
        except Exception as exc:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"ECMWF falhou em {date} {cycle}Z F{lead:03d}: {exc}") from exc


def download_gfs(date: str, cycle: str, hours: list[int], root: Path) -> None:
    session = _session()
    delay = max(0.0, float(os.getenv("GFS_REQUEST_DELAY_SECONDS", "10")))
    base_url = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
    for index, lead in enumerate(hours):
        path = raw_path(root, "gfs", date, cycle, lead)
        if path.exists() and path.stat().st_size > 100:
            LOG.info("GFS F%03d já existe; mantendo cache", lead)
            continue
        filename = f"gfs.t{cycle}z.pgrb2.0p25.f{lead:03d}"
        params = {
            "file": filename,
            "dir": f"/gfs.{date}/{cycle}/atmos",
            "var_TMP": "on", "lev_2_m_above_ground": "on",
            "var_DPT": "on", "lev_2_m_above_ground": "on",
            "var_UGRD": "on", "lev_10_m_above_ground": "on",
            "var_VGRD": "on", "lev_10_m_above_ground": "on",
            "var_PRMSL": "on", "lev_mean_sea_level": "on",
            "var_APCP": "on", "lev_surface": "on",
        }
        LOG.info("NOAA GFS: baixando rodada %s %sZ F%03d", date, cycle, lead)
        response = _get(session, base_url, params=params)
        content = _decompress(response.content, response.url)
        if not content.startswith(b"GRIB"):
            preview = content[:160].decode("utf-8", "replace").replace("\n", " ")
            raise RuntimeError(f"NOMADS não retornou GRIB2 para F{lead:03d}; resposta: {preview}")
        _write_bytes(path, content)
        if index != len(hours) - 1 and delay:
            time.sleep(delay)


def _directory_files(session: requests.Session, directory_url: str) -> list[str]:
    response = _get(session, directory_url)
    # Índice Apache simples; usar HTML parser evitava dependência adicional, mas os hrefs
    # dos arquivos são URLs relativas e podem ser extraídos com regex.
    return re.findall(r'href=["\']([^"\']+)["\']', response.text, flags=re.IGNORECASE)


def download_icon(date: str, cycle: str, hours: list[int], root: Path) -> None:
    session = _session()
    base = f"https://opendata.dwd.de/weather/nwp/icon/grib/{cycle}/"
    cache: dict[str, list[str]] = {}
    for lead in hours:
        path = raw_path(root, "icon", date, cycle, lead)
        if path.exists() and path.stat().st_size > 100:
            LOG.info("ICON F%03d já existe; mantendo cache", lead)
            continue
        chunks: list[bytes] = []
        for _field, (folder, code) in ICON_FIELDS.items():
            directory_url = urljoin(base, folder + "/")
            if directory_url not in cache:
                cache[directory_url] = _directory_files(session, directory_url)
            # Exemplo de nome oficial: icon_global_icosahedral_single-level_YYYYMMDDHH_006_T_2M.grib2.bz2
            pattern = re.compile(
                rf"{re.escape(date + cycle)}_{lead:03d}_{re.escape(code)}\.grib2(?:\.bz2|\.gz)?$",
                re.IGNORECASE,
            )
            match = next((name for name in cache[directory_url] if pattern.search(name)), None)
            if not match:
                raise RuntimeError(f"DWD ICON não publicou {folder} para {date}{cycle} F{lead:03d}")
            url = urljoin(directory_url, match)
            LOG.info("DWD ICON: %s F%03d", folder, lead)
            response = _get(session, url)
            raw = _decompress(response.content, response.url)
            if not raw.startswith(b"GRIB"):
                raise RuntimeError(f"Resposta ICON inesperada para {url}; não parece GRIB2")
            chunks.append(raw)
        _write_bytes(path, b"".join(chunks))


def download_all(date: str, cycle: str, hours: list[int], root: str | Path, models: list[str] | None = None) -> None:
    root = Path(root)
    selected = models or list(MODEL_IDS)
    unknown = sorted(set(selected) - set(MODEL_IDS))
    if unknown:
        raise ValueError(f"Modelos desconhecidos: {', '.join(unknown)}")
    functions = {
        "ecmwf": download_ecmwf,
        "gfs": download_gfs,
        "icon": download_icon,
    }
    errors: list[str] = []
    for model in selected:
        try:
            LOG.info("Iniciando download: %s", model.upper())
            functions[model](date, cycle, hours, root)
            LOG.info("Download concluído: %s", model.upper())
        except Exception as exc:
            errors.append(f"{model.upper()}: {exc}")
            LOG.error("Falha no modelo %s: %s", model.upper(), exc)
    if errors:
        raise RuntimeError("Um ou mais modelos não foram obtidos; o consenso não foi publicado:\n- " + "\n- ".join(errors))
