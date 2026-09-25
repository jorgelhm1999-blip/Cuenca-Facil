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
                       and not any(part.startswith('pasada_') or part == 'preparacion'
                                   for part in p.relative_to(folder).parts)
                       and p.stem != 'cuenca_mascara'):
        try:
            with rasterio.open(path) as ds:
                if ds.count != 1 or not ds.crs or not ds.crs.is_projected:
                    raise ValueError('necesita una sola banda de elevación y un CRS proyectado')
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


def neighboring_tiles(tiles: list[Tile], selected: set[Path]) -> list[Tile]:
    """Compatible catalog tiles sharing an edge or corner with the selected mosaic."""
    if not selected:
        return []
    lookup = {t.path: t for t in tiles}
    anchor = lookup[next(iter(selected))]
    tolerance = min(anchor.resolution)*.1
    found = []
    for tile in tiles:
        if tile.path in selected or not compatible(anchor, tile):
            continue
        for chosen in selected:
            a, b = tile.bounds, lookup[chosen].bounds
            if (a[0] <= b[2]+tolerance and a[2] >= b[0]-tolerance and
                    a[1] <= b[3]+tolerance and a[3] >= b[1]-tolerance):
                found.append(tile)
                break
    return sorted(found, key=lambda t: str(t.path))


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
    logfile = wd / 'registro_motor.txt'
    recent, diagnostic = [], []
    with logfile.open('a', encoding='utf-8') as record:
        record.write('\n=== ' + name + ' ===\n')
        record.write('Ejecutable: ' + str(exe) + '\n')
        record.flush()
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                record.write(line + '\n')
                recent.append(line)
                recent = recent[-30:]
                if any(word in line.lower() for word in
                       ('error', 'panic', 'failed', 'cannot', 'no space', 'not enough', 'warning')):
                    diagnostic.append(line)
                    log(line)
        code = proc.wait()
        record.write(f'Código de salida: {code} (0x{code & 0xffffffff:08X})\n')
    if code:
        output_file = next((Path(arg.split('=', 1)[1]) for arg in args
                            if arg.startswith('--output=')), None)
        free_gb = shutil.disk_usage(wd).free / 1_000_000_000
        outfile = (f'{output_file.name}: {output_file.stat().st_size/1_000_000:.1f} MB'
                   if output_file and output_file.exists() else 'No se creó el fichero de salida')
        detail = '\n'.join(diagnostic[-8:] or recent[-4:])
        raise RuntimeError(f'{name} ha fallado (código {code}, 0x{code & 0xffffffff:08X}).\n'
                           f'{detail}\n{outfile}. Espacio libre: {free_gb:.1f} GB.\n'
                           f'Registro completo: {logfile}')


def write_mosaic(tiles: list[Tile], path: Path) -> None:
    """Rasterio writes each chunk directly to disk; never merges whole tiles in RAM."""
    with ExitStack() as stack:
        datasets = [stack.enter_context(rasterio.open(t.path)) for t in tiles]
        merge(datasets, indexes=1, dtype='float32', nodata=-9999.0,
              dst_path=path, mem_limit=64,
              dst_kwds={'compress': 'deflate', 'tiled': True, 'blockxsize': 256,
                        'blockysize': 256, 'bigtiff': 'IF_SAFER', 'predictor': 1})


def hydro_paths(work: Path) -> tuple[Path, Path, Path, Path]:
    return tuple(work / name for name in ('mosaico.tif', 'mdt_corregido.tif',
                                          'direccion_d8.tif', 'acumulacion_celdas.tif'))


def _cache_key(seed: Tile, exe: Path, breach_cells: int) -> dict:
    stat = seed.path.stat()
    return {'tesela': str(seed.path), 'tamano': stat.st_size,
            'modificacion_ns': stat.st_mtime_ns, 'motor': str(exe.resolve()),
            'brecha_celdas': breach_cells}


def cached_hydrology(work: Path, seed: Tile, exe: Path, breach_cells: int) -> bool:
    try:
        manifest = json.loads((work/'preparacion.json').read_text(encoding='utf-8'))
        return manifest == _cache_key(seed, exe, breach_cells) and all(
            path.is_file() and path.stat().st_size > 0 for path in hydro_paths(work))
    except (OSError, ValueError):
        return False


def prepare_hydrology(seed: Tile, out: Path, whitebox: Path, breach_cells: int = 50,
                      log: Log = print) -> Path:
    """Produce a preliminary D8 network before asking the user for an outlet."""
    if breach_cells < 1:
        raise ValueError('La búsqueda de brechas debe ser mayor que cero.')
    out = Path(out).resolve()
    work = out/'preparacion'
    work.mkdir(parents=True, exist_ok=True)
    exe = locate_whitebox(str(whitebox))
    dem, prepared, pntr, accum = hydro_paths(work)
    if cached_hydrology(work, seed, exe, breach_cells):
        log('Reutilizando la red de drenaje ya calculada para esta tesela.')
        return accum
    (work/'preparacion.json').unlink(missing_ok=True)
    log('Preparando MDT y red de drenaje de ' + seed.path.name)
    write_mosaic([seed], dem)
    run_tool(exe, 'BreachDepressionsLeastCost', work,
             [f'--dem={dem}', f'--output={prepared}', f'--dist={breach_cells}', '--fill'], log)
    run_tool(exe, 'D8Pointer', work, [f'--dem={prepared}', f'--output={pntr}'], log)
    run_tool(exe, 'D8FlowAccumulation', work,
             [f'--input={pntr}', f'--output={accum}', '--out_type=cells', '--pntr'], log)
    (work/'preparacion.json').write_text(
        json.dumps(_cache_key(seed, exe, breach_cells), ensure_ascii=False, indent=2),
        encoding='utf-8')
    return accum


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


def snap_to_stream(path: Path, point: tuple[float, float], radius_m: float,
                   area_threshold_km2: float):
    """Pick the nearest qualifying stream cell; avoid drifting downstream to max flow."""
    if radius_m < 0:
        raise ValueError('El radio de ajuste no puede ser negativo.')
    if area_threshold_km2 <= 0:
        raise ValueError('El umbral de cauce debe ser mayor que cero.')
    with rasterio.open(path) as ds:
        row, col = ds.index(*point)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            raise ValueError('Punto fuera del MDT preparado.')
        rx = math.ceil(radius_m/ds.res[0]) + 1
        ry = math.ceil(radius_m/ds.res[1]) + 1
        c0, c1 = max(0, col-rx), min(ds.width, col+rx+1)
        r0, r1 = max(0, row-ry), min(ds.height, row+ry+1)
        a = ds.read(1, window=Window(c0, r0, c1-c0, r1-r0), masked=True)
        yy, xx = np.mgrid[r0:r1, c0:c1]
        xs = ds.transform.c+(xx+.5)*ds.transform.a
        ys = ds.transform.f+(yy+.5)*ds.transform.e
        distance = np.hypot(xs-point[0], ys-point[1])
        required = area_threshold_km2*1_000_000/abs(ds.transform.a*ds.transform.e)
        eligible = (a.data >= required) & (~np.ma.getmaskarray(a)) & np.isfinite(a.data) & (distance <= radius_m)
        if not np.any(eligible):
            raise ValueError('No hay un cauce calculado dentro del radio: acerca el visor, '
                             'reduce el umbral de área o amplía el radio.')
        # Nearest cell first; at equal distance prefer the larger contributing area.
        choice = np.argmin(np.where(eligible, distance - np.minimum(a.data, 1e9)*1e-12, np.inf))
        rr, cc = np.unravel_index(choice, a.shape)
        return ((float(xs[rr, cc]), float(ys[rr, cc])), int(a.data[rr, cc]),
                (int(yy[rr, cc]), int(xx[rr, cc])))


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
              log: Log = print, fixed_point: tuple[float, float] | None = None,
              forced_tiles: list[Path] | None = None) -> dict:
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
    forced = set(forced_tiles or [])
    if not forced.issubset(lookup):
        raise ValueError('Hay teselas de auditoría fuera del catálogo.')
    if any(not compatible(base, lookup[p]) for p in forced):
        raise ValueError('Las teselas de auditoría deben tener el mismo CRS y retícula.')
    selected = {seed} | forced
    exe = locate_whitebox(str(whitebox))
    result = {}
    if fixed_point and math.dist(point, fixed_point) > snap_radius + 1e-7:
        raise ValueError('El punto confirmado excede el radio máximo de ajuste.')
    for iteration in range(len(tiles)+1):
        cached = iteration == 0 and not forced and cached_hydrology(out/'preparacion', base, exe, breach_cells)
        work = out/'preparacion' if cached else out / f'pasada_{iteration+1:02d}'
        work.mkdir(exist_ok=True)
        dem, prepared, pntr, accum = hydro_paths(work)
        log('Teselas: ' + ', '.join(p.name for p in sorted(selected)))
        if cached:
            log('Reutilizando los cálculos hidrológicos del visor de cauces.')
        else:
            write_mosaic([lookup[p] for p in sorted(selected)], dem)
            run_tool(exe, 'BreachDepressionsLeastCost', work,
                     [f'--dem={dem}', f'--output={prepared}', f'--dist={breach_cells}', '--fill'], log)
            run_tool(exe, 'D8Pointer', work, [f'--dem={prepared}', f'--output={pntr}'], log)
            run_tool(exe, 'D8FlowAccumulation', work,
                     [f'--input={pntr}', f'--output={accum}', '--out_type=cells', '--pntr'], log)
        if fixed_point is None:
            snapped, contributing, cell = snap_to_flow(accum, point, snap_radius)
            fixed_point = snapped
        else:
            snapped, contributing, cell = snap_to_flow(accum, fixed_point, 0)
            if math.dist(snapped, fixed_point) > 1e-5:
                raise RuntimeError('El mosaico ampliado no conserva la retícula del punto confirmado.')
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
                  'iteraciones': iteration+1, 'directorio_final': str(work),
                  'teselas_forzadas': [str(p) for p in sorted(forced)],
                  'teselas_vecinas_no_analizadas': [str(t.path) for t in neighboring_tiles(tiles, selected)],
                  'motor': 'WhiteboxTools: BreachDepressionsLeastCost, D8Pointer, D8FlowAccumulation, Watershed'}
        if not additions:
            export_results(out, basin, dem, result)
            log(f'Área obtenida: {result["area_km2"]:.4f} km².')
            if not result['resultado_completo']:
                log('ATENCIÓN: la cuenca alcanza un borde sin MDT o una tesela incompatible; resultado provisional.')
            return result
        log('La cuenca alcanza el borde: incorporando ' + ', '.join(p.name for p in sorted(additions)))
        selected.update(additions)
    raise RuntimeError('No se pudo completar la expansión de teselas.')


def compare_basins(original: Path, audited: Path, destination: Path) -> dict:
    """Compare both basin rasters on the audit grid; report gained and lost pixels."""
    from rasterio.vrt import WarpedVRT
    destination = Path(destination)
    original = Path(original)
    audited = Path(audited)
    with rasterio.open(original) as old, rasterio.open(audited) as new:
        if old.crs != new.crs or old.res != new.res:
            raise ValueError('No coinciden las proyecciones o resoluciones de las dos cuencas.')
        profile = new.profile.copy()
        profile.update(dtype='uint8', count=1, nodata=0, compress='deflate', tiled=True,
                       blockxsize=256, blockysize=256)
        only_old = only_new = common = 0
        with WarpedVRT(old, crs=new.crs, transform=new.transform, width=new.width,
                       height=new.height, resampling=Resampling.nearest,
                       nodata=0, src_nodata=0) as on_new, rasterio.open(destination, 'w', **profile) as diff:
            for _, win in new.block_windows(1):
                old_arr = on_new.read(1, window=win) > 0
                new_arr = new.read(1, window=win) > 0
                gained = new_arr & ~old_arr
                lost = old_arr & ~new_arr
                only_new += int(gained.sum())
                only_old += int(lost.sum())
                common += int((old_arr & new_arr).sum())
                diff.write((gained.astype('uint8')*2 + lost.astype('uint8')), 1, window=win)
        pixel_area = abs(new.transform.a*new.transform.e)
        return {'area_original_km2': (common+only_old)*pixel_area/1e6,
                'area_auditoria_km2': (common+only_new)*pixel_area/1e6,
                'solo_original_km2': only_old*pixel_area/1e6,
                'solo_auditoria_km2': only_new*pixel_area/1e6,
                'diferencia_simetrica_km2': (only_old+only_new)*pixel_area/1e6,
                'coincidencia_porcentaje': 100*common/max(1, common+only_old+only_new)}
