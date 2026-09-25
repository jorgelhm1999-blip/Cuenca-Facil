"""End-to-end test with two adjacent 2 m DEM tiles and a real WhiteboxTools binary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from types import SimpleNamespace
from unittest.mock import patch
from io import BytesIO

import fiona
import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from engine import (compare_basins, delineate, index_tiles, locate_whitebox,
                    neighboring_tiles, prepare_hydrology, snap_to_flow, snap_to_stream)
from app import Window
from ortho import local_ortho_image, pnoa_image
from diagnostics import diagnostic_preview


def main():
    exe = locate_whitebox(sys.argv[1] if len(sys.argv) > 1 else None)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for i in range(2):
            rows, cols = np.mgrid[0:60, 0:60]
            elevations = (1000 - (rows+i*60)*.5 + abs(cols-30)*.12).astype('float32')
            with rasterio.open(root/f'mdt_{i}.tif', 'w', driver='GTiff',
                               width=60, height=60, count=1, dtype='float32',
                               crs='EPSG:25830', transform=from_origin(500000, 4400000-i*120, 2, 2),
                               nodata=-9999) as ds:
                ds.write(elevations, 1)
        tiles, errors = index_tiles(root)
        assert len(tiles) == 2 and not errors
        assert neighboring_tiles(tiles, {tiles[1].path}) == [tiles[0]]
        rgb = np.full((3, 60, 60), 120, dtype='uint8')
        with rasterio.open(root/'foto.tif', 'w', driver='GTiff', width=60, height=60,
                           count=3, dtype='uint8', crs='EPSG:25830',
                           transform=from_origin(500000, 4400000, 2, 2)) as ortho:
            ortho.write(rgb)
        assert local_ortho_image(root/'foto.tif', tiles[0].bounds,
                                 'EPSG:25830', 60, 60).size == (60, 60)
        # PNOA test uses a mocked response: no live Internet dependency.
        payload = BytesIO()
        Image.new('RGB', (24, 18), (120, 80, 40)).save(payload, 'JPEG')
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, length): return payload.getvalue()
        with patch('ortho.urlopen', return_value=Response()) as request:
            assert pnoa_image(tiles[0].bounds, 'EPSG:25830', 24, 18).size == (24, 18)
            assert 'SRS=EPSG%3A25830' in request.call_args.args[0].full_url
        output = root/'salida'
        accumulation = prepare_hydrology(tiles[1], output, exe, breach_cells=10,
                                         log=lambda text: None)
        assert accumulation.is_file()
        direction = accumulation.parent/'direccion_d8.tif'
        assert diagnostic_preview(direction, 'direction', 60, 60).size == (60, 60)
        assert diagnostic_preview(accumulation, 'accumulation', 60, 60).size == (60, 60)
        preview = SimpleNamespace(accum_path=accumulation, preview=Image.new('RGB', (60, 60)),
                                  threshold=SimpleNamespace(get=lambda: '0,0001'),
                                  draw=lambda: None, _log=lambda text: None, stream_overlay=None,
                                  outlet=None)
        Window.refresh_stream_overlay(preview)
        assert preview.stream_overlay.getchannel('A').getbbox() is not None
        snapped, cells, _ = snap_to_flow(accumulation, (500060, 4399770), 4)
        assert cells > 1 and np.hypot(snapped[0]-500060, snapped[1]-4399770) <= 4
        stream_point, stream_cells, _ = snap_to_stream(accumulation, (500060, 4399770),
                                                        4, .0001)
        assert stream_cells >= 25
        assert np.hypot(stream_point[0]-500060, stream_point[1]-4399770) <= 4
        result = delineate(tiles, tiles[1].path, (500060, 4399770), output, exe,
                           snap_radius=4, breach_cells=10, log=lambda text: None,
                           fixed_point=stream_point)
        assert result['iteraciones'] == 2
        assert len(result['teselas']) == 2
        assert result['cells'] > 60
        assert result['area_m2'] == result['cells']*4
        assert result['desplazamiento_m'] <= 4
        assert tuple(result['punto_ajustado']) == stream_point
        assert (output/'preparacion'/'registro_motor.txt').read_text().count('=== BreachDepressionsLeastCost ===') == 1
        assert not result['resultado_completo']  # cuenca abierta aguas arriba
        with rasterio.open(output/'cuenca_mascara.tif') as ds:
            assert ds.crs.to_epsg() == 25830
            assert ds.read(1).sum() == result['cells']
        with fiona.open(output/'cuenca.gpkg') as gpkg:
            assert len(gpkg) >= 1 and gpkg.crs.to_epsg() == 25830
        assert not neighboring_tiles(tiles, {Path(p) for p in result['teselas']})
        second = output/'cuenca_alternativa.tif'
        with rasterio.open(output/'cuenca_mascara.tif') as src:
            original = src.read(1)
            modified = original.copy()
            modified[0, 0] = 1 - modified[0, 0]
            with rasterio.open(second, 'w', **src.profile) as dst: dst.write(modified, 1)
        comparison = compare_basins(output/'cuenca_mascara.tif', second,
                                    output/'diferencias.tif')
        assert abs(comparison['diferencia_simetrica_km2'] - 4e-6) < 1e-12
        print('OK: red, expansión, máscara, comparación espacial y ortofotos.')


if __name__ == '__main__':
    main()
