"""End-to-end test with two adjacent 2 m DEM tiles and a real WhiteboxTools binary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import fiona
import numpy as np
import rasterio
from rasterio.transform import from_origin

from engine import delineate, index_tiles, locate_whitebox


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
        output = root/'salida'
        result = delineate(tiles, tiles[1].path, (500060, 4399770), output, exe,
                           snap_radius=4, breach_cells=10, log=lambda text: None)
        assert result['iteraciones'] == 2
        assert len(result['teselas']) == 2
        assert result['cells'] > 60
        assert result['area_m2'] == result['cells']*4
        assert result['desplazamiento_m'] <= 4
        assert not result['resultado_completo']  # cuenca abierta aguas arriba
        with rasterio.open(output/'cuenca_mascara.tif') as ds:
            assert ds.crs.to_epsg() == 25830
            assert ds.read(1).sum() == result['cells']
        with fiona.open(output/'cuenca.gpkg') as gpkg:
            assert len(gpkg) >= 1 and gpkg.crs.to_epsg() == 25830
        print('OK: expansión entre teselas, ajuste, área, máscara y GeoPackage.')


if __name__ == '__main__':
    main()
