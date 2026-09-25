"""Local, disk-backed watershed delineation. WhiteboxTools does the full-grid work."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.merge import merge
from rasterio.windows import Window
from scipy.ndimage import binary_dilation

Log = Callable[[str], None]


@dataclass(frozen=True)
class Tile:
    path: Path
    bounds: tuple[float, float, float, float]
    crs: str
    resolution: tuple[float, float]
    origin: tuple[float, float]
    width: int
    height: int
    nodata: float | None


def index_tiles(folder: Path) -> tuple[list[Tile], list[str]]:
    """Index TIFF headers, never loading their pixel arrays."""
    folder = Path(folder).resolve()
    tiles, errors = [], []
    for path in sorted(p for p in folder.rglob('*') if p.suffix.lower() in ('.tif', '.tiff')
                       and not any(part.startswith('pasada_') for part in p.relative_to(folder).parts)
                       and p.stem != 'cuenca_mascara'):
        try:
            with rasterio.open(path) as ds:
                if ds.count < 1 or not ds.crs or not ds.crs.is_projected:
                    raise ValueError('necesita una banda y un CRS proyectado')
                if not math.isclose(ds.crs.linear_units_factor[1], 1.0, rel_tol=1e-7):
                    raise ValueError('el CRS horizontal debe estar en metros')
                t = ds.transform
                if abs(t.b) > 1e-9 or abs(t.d) > 1e-9 or t.a <= 0 or t.e >= 0:
                    raise ValueError('retícula rotada o invertida')
                if ds.width < 2 or ds.height < 2:
                    raise ValueError('ráster demasiado pequeño')
                tiles.append(Tile(path, tuple(ds.bounds), ds.crs.to_string(),
                                  (t.a, -t.e), (t.c, t.f), ds.width, ds.height, ds.nodata))
        except Exception as exc:
            errors.append(f'{path.name}: {exc}')
    return tiles, errors


def compatible(base: Tile, other: Tile) -> bool:
    if base.crs != other.crs:
        return False
    for a, b in zip(base.resolution, other.resolution):
        if not math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-7):
            return False
    for i in (0, 1):
        delta = (other.origin[i] - base.origin[i]) / base.resolution[i]
        if abs(delta - round(delta)) > 1e-5:
            return False
    return True


def locate_whitebox(explicit: str | None = None) -> Path:
    import sys
    names = ['whitebox_tools.exe', 'whitebox_tools']
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    choices = ([Path(explicit)] if explicit else []) + [
        root / 'bin' / name for name in names
    ] + [Path(p) for name in names if (p := shutil.which(name))]
    for choice in choices:
        if choice.is_file():
            return choice.resolve()
    raise FileNotFoundError('Falta whitebox_tools. Selecciona el ejecutable en la ventana.')


def run_tool(exe: Path, name: str, wd: Path, args: list[str], log: Log) -> None:
    command = [str(exe), f'--run={name}', f'--wd={wd}', *args]
    log(f'Analizando: {name}')
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    recent = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line:
            recent.append(line)
            recent = recent[-12:]
            if 'error' in line.lower() or 'warning' in line.lower():
                log(line)
    if proc.wait() != 0:
        raise RuntimeError(f'{name} ha fallado:\n' + '\n'.join(recent))


def write_mosaic(tiles: list[Tile], path: Path) -> None:
    """Rasterio writes each chunk directly to disk; never merges whole tiles in RAM."""
    with ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(t.path)) for t in tiles]
        merge(datasets, indexes=1, dtype='float32', nodata=-9999.0,
              dst_path=path, mem_limit=64,
              dst_kwds={'compress': 'deflate', 'tiled': True, 'blockxsize': 256,
                        'blockysize': 256, 'bigtiff': 'IF_SAFER', 'predictor': 1})


def snap_to_flow(path: Path, point: tuple[float, float], radius_m: float):
    with rasterio.open(path) as ds:
        row, col = ds.index(*point)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            raise ValueError('El punto de vertido está fuera de las teselas cargadas.')
        rx = max(0, math.ceil(radius_m / ds.res[0]))
        ry = max(0, math.ceil(radius_m / ds.res[1]))
        c0, c1 = max(0, col-rx), min(ds.width, col+rx+1)
        r0, r1 = max(0, row-ry), min(ds.height, row+ry+1)
        a = ds.read(1, window=Window(c0, r0, c1-c0, r1-r0), masked=True)
        yy, xx = np.mgrid[r0:r1, c0:c1]
        xcentres = ds.transform.c + (xx + .5)*ds.transform.a
        ycentres = ds.transform.f + (yy + .5)*ds.transform.e
        distances = np.hypot(xcentres-point[0], ycentres-point[1])
        valid = np.isfinite(a.data) & (~np.ma.getmaskarray(a)) & (distances <= radius_m + 1e-8)
        if not np.any(valid):
            raise ValueError('No hay celdas válidas cerca del punto. Revisa el MDT.')
        maximum = np.max(a.data[valid])
        choices = np.where(valid & (a.data == maximum))
        which = np.argmin(distances[choices])
        new_row, new_col = int(yy[choices][which]), int(xx[choices][which])
        x, y = ds.xy(new_row, new_col)
        return (x, y), int(maximum), (new_row, new_col)


def pour_raster(pointer: Path, cell: tuple[int, int], destination: Path) -> None:
    with rasterio.open(pointer) as ds:
        profile = ds.profile.copy()
        profile.update(dtype='int32', count=1, nodata=0, compress='deflate')
        with rasterio.open(destination, 'w', **profile) as out:
            for _, window in out.block_windows(1):
                out.write(np.zeros((int(window.height), int(window.width)), dtype=np.int32),
                          1, window=window)
            out.write(np.array([[1]], dtype=np.int32), 1, window=Window(cell[1], cell[0], 1, 1))


def frontier(watershed: Path, dem: Path, all_tiles: list[Tile], selected: set[Path], base: Tile):
    """Find DEM gaps touching the actual basin, including exterior edges/corners."""
    additions: set[Path] = set()
    incompatible: set[Path] = set()
    uncovered = 0
    with rasterio.open(watershed) as basin, rasterio.open(dem) as surface:
        if basin.shape != surface.shape or basin.transform != surface.transform:
            raise RuntimeError('La máscara de cuenca no coincide con el mosaico MDT.')
        alternatives = [t for t in all_tiles if t.path not in selected]

        def classify(rows, cols):
            nonlocal uncovered
            if not len(rows):
                return
            xs = basin.transform.c + (cols + .5)*basin.transform.a
            ys = basin.transform.f + (rows + .5)*basin.transform.e
            covered = np.zeros(len(rows), dtype=bool)
            for tile in alternatives:
                left, bottom, right, top = tile.bounds
                inside = (xs >= left) & (xs < right) & (ys > bottom) & (ys <= top)
                if np.any(inside):
                    covered |= inside
                    (additions if compatible(base, tile) else incompatible).add(tile.path)
            uncovered += int((~covered).sum())

        for _, win in basin.block_windows(1):
            r, c, h, w = map(int, (win.row_off, win.col_off, win.height, win.width))
            expanded = Window(c-1, r-1, w+2, h+2)
            data = basin.read(1, window=expanded, boundless=True, fill_value=basin.nodata or 0)
            included = (data > 0) & np.isfinite(data)
            valid = surface.read_masks(1, window=expanded, boundless=True) > 0
            gaps = binary_dilation(included, structure=np.ones((3, 3), bool))[1:-1, 1:-1] & ~valid[1:-1, 1:-1]
            rr, cc = np.where(gaps)
            classify(r+rr, c+cc)
            # Check the first row/column *outside* the raster as well as its corners.
            if r == 0:
                cols = np.flatnonzero(included[1, 1:-1]) + c
                classify(np.full(len(cols), -1), cols)
            if r+h == basin.height:
                cols = np.flatnonzero(included[-2, 1:-1]) + c
                classify(np.full(len(cols), basin.height), cols)
            if c == 0:
                rows = np.flatnonzero(included[1:-1, 1]) + r
                classify(rows, np.full(len(rows), -1))
            if c+w == basin.width:
                rows = np.flatnonzero(included[1:-1, -2]) + r
                classify(rows, np.full(len(rows), basin.width))
            for dr, dc, br, bc in ((-1, -1, 0, 0), (-1, 1, 0, basin.width-1),
                                    (1, -1, basin.height-1, 0),
                                    (1, 1, basin.height-1, basin.width-1)):
                if r <= br < r+h and c <= bc < c+w and included[br-r+1, bc-c+1]:
                    classify(np.array([br+dr]), np.array([bc+dc]))
    return additions, incompatible, uncovered


def export_results(folder: Path, watershed: Path, dem: Path, metadata: dict) -> None:
    """Create a georeferenced binary mask and a projected GeoPackage polygon."""
    import fiona
    mask_file = folder / 'cuenca_mascara.tif'
    polygon_file = folder / 'cuenca.gpkg'
    with rasterio.open(watershed) as src:
        profile = src.profile.copy()
        profile.update(dtype='uint8', nodata=0, compress='deflate', tiled=True,
                       blockxsize=256, blockysize=256, count=1)
        count = 0
        with rasterio.open(mask_file, 'w', **profile) as dst:
            for _, win in src.block_windows(1):
                a = src.read(1, window=win, masked=True)
                mask = ((a.data > 0) & (~np.ma.getmaskarray(a)) & np.isfinite(a.data)).astype('uint8')
                count += int(mask.sum())
                dst.write(mask, 1, window=win)
        if count == 0:
            raise RuntimeError('WhiteboxTools ha devuelto una cuenca vacía.')
        metadata['area_m2'] = float(count * abs(src.transform.a * src.transform.e))
        metadata['area_km2'] = metadata['area_m2'] / 1_000_000
        metadata['cells'] = count
        metadata['crs'] = src.crs.to_string()
        schema = {'geometry': 'Polygon', 'properties': {'id': 'int', 'area_km2': 'float'}}
        with rasterio.open(mask_file) as binary, fiona.open(
            polygon_file, 'w', driver='GPKG', layer='cuenca', crs_wkt=src.crs.to_wkt(), schema=schema
        ) as vector:
            # rasterio.band streams pixels from disk to polygonization.
            for geom, value in shapes(rasterio.band(binary, 1),
                                      mask=rasterio.band(binary, 1), transform=binary.transform):
                if value == 1:
                    if geom['type'] == 'Polygon':
                        vector.write({'geometry': geom, 'properties': {'id': 1, 'area_km2': metadata['area_km2']}})
                    elif geom['type'] == 'MultiPolygon':
                        for ring in geom['coordinates']:
                            vector.write({'geometry': {'type': 'Polygon', 'coordinates': ring},
                                          'properties': {'id': 1, 'area_km2': metadata['area_km2']}})
    with (folder / 'resultado.json').open('w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def delineate(tiles: list[Tile], seed: Path, point: tuple[float, float], out: Path,
              whitebox: Path, snap_radius: float = 12, breach_cells: int = 50,
              log: Log = print) -> dict:
    if not tiles or seed not in {t.path for t in tiles}:
        raise ValueError('Selecciona una tesela indexada.')
    if snap_radius < 0 or breach_cells < 1:
        raise ValueError('Distancia de ajuste y búsqueda de brecha inválidas.')
    lookup = {t.path: t for t in tiles}
    base = lookup[seed]
    if not (base.bounds[0] <= point[0] < base.bounds[2] and
            base.bounds[1] < point[1] <= base.bounds[3]):
        raise ValueError('El punto debe estar en la tesela inicial.')
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    selected = {seed}
    exe = locate_whitebox(str(whitebox))
    result = {}
    for iteration in range(len(tiles)+1):
        work = out / f'pasada_{iteration+1:02d}'
        work.mkdir(exist_ok=True)
        dem, prepared, pntr, accum = [work / f for f in ('mosaico.tif', 'mdt_corregido.tif',
                                                        'direccion_d8.tif', 'acumulacion_celdas.tif')]
        log('Teselas: ' + ', '.join(p.name for p in sorted(selected)))
        write_mosaic([lookup[p] for p in sorted(selected)], dem)
        run_tool(exe, 'BreachDepressionsLeastCost', work,
                 [f'--dem={dem}', f'--output={prepared}', f'--dist={breach_cells}', '--fill'], log)
        run_tool(exe, 'D8Pointer', work, [f'--dem={prepared}', f'--output={pntr}'], log)
        run_tool(exe, 'D8FlowAccumulation', work,
                 [f'--input={pntr}', f'--output={accum}', '--out_type=cells', '--pntr'], log)
        snapped, contributing, cell = snap_to_flow(accum, point, snap_radius)
        log(f'Punto ajustado a X={snapped[0]:.2f}, Y={snapped[1]:.2f}; '
            f'desplazamiento {math.dist(point, snapped):.2f} m.')
        pour = work / 'punto_vertido.tif'
        pour_raster(pntr, cell, pour)
        basin = work / 'cuenca_bruta.tif'
        run_tool(exe, 'Watershed', work,
                 [f'--d8_pntr={pntr}', f'--pour_pts={pour}', f'--output={basin}'], log)
        additions, incompatible, uncovered = frontier(basin, dem, tiles, selected, base)
        result = {'punto_inicial': list(point), 'punto_ajustado': list(snapped),
                  'desplazamiento_m': math.dist(point, snapped), 'acumulacion_celdas': contributing,
                  'radio_ajuste_m': snap_radius, 'busqueda_brecha_celdas': breach_cells,
                  'teselas': [str(p) for p in sorted(selected)], 'teselas_incompatibles':
                  [str(p) for p in sorted(incompatible)], 'bordes_sin_cobertura': uncovered,
                  'resultado_completo': not (incompatible or uncovered),
                  'iteraciones': iteration+1, 'motor': 'WhiteboxTools: BreachDepressionsLeastCost, D8Pointer, D8FlowAccumulation, Watershed'}
        if not additions:
            export_results(out, basin, dem, result)
            log(f'Área obtenida: {result["area_km2"]:.4f} km².')
            if not result['resultado_completo']:
                log('ATENCIÓN: la cuenca alcanza un borde sin MDT o una tesela incompatible; resultado provisional.')
            return result
        log('La cuenca alcanza el borde: incorporando ' + ', '.join(p.name for p in sorted(additions)))
        selected.update(additions)
    raise RuntimeError('No se pudo completar la expansión de teselas.')
