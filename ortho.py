"""PNOA WMS and offline GeoTIFF orthophoto backgrounds for the local GUI."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

PNOA_WMS = 'https://www.ign.es/wms-inspire/pnoa-ma'


def pnoa_image(bounds, crs, width, height) -> Image.Image:
    """Fetch only the low-resolution screen image; source DEM never leaves the PC."""
    if not crs or not rasterio.crs.CRS.from_user_input(crs).to_epsg():
        raise ValueError('La ortofoto PNOA necesita un código EPSG reconocido.')
    parameters = {
        'SERVICE': 'WMS', 'VERSION': '1.1.1', 'REQUEST': 'GetMap',
        'LAYERS': 'OI.OrthoimageCoverage', 'STYLES': '',
        'FORMAT': 'image/jpeg', 'SRS': f'EPSG:{rasterio.crs.CRS.from_user_input(crs).to_epsg()}',
        'BBOX': ','.join(f'{v:.3f}' for v in bounds),
        'WIDTH': int(width), 'HEIGHT': int(height),
    }
    request = Request(PNOA_WMS + '?' + urlencode(parameters),
                      headers={'User-Agent': 'CuencaFacil/0.4 (cartografia local)'})
    with urlopen(request, timeout=25) as response:
        payload = response.read(18_000_000)
    try:
        with Image.open(BytesIO(payload)) as picture:
            image = picture.convert('RGB')
    except Exception as exc:
        raise RuntimeError('El servicio PNOA no devolvió una imagen: ' +
                           payload[:280].decode('utf-8', errors='replace')) from exc
    if image.size != (width, height):
        raise RuntimeError('La ortofoto recibida no tiene el tamaño esperado.')
    return image


def local_ortho_image(path: Path, bounds, crs, width, height) -> Image.Image:
    """Read a local, georeferenced RGB raster into the exact DEM preview extent."""
    with rasterio.open(path) as source:
        if source.count < 3 or not source.crs:
            raise ValueError('La ortofoto local debe ser un GeoTIFF RGB georreferenciado.')
        transform = from_bounds(*bounds, width, height)
        with WarpedVRT(source, crs=crs, transform=transform, width=width,
                       height=height, resampling=Resampling.bilinear, nodata=0) as vrt:
            bands = vrt.read([1, 2, 3], masked=True)
            if bands.dtype == np.uint8:
                rgb = bands.filled(0).astype('uint8')
            else:
                rgb = np.zeros(bands.shape, dtype='uint8')
                for i in range(3):
                    valid = bands[i].compressed()
                    if not len(valid): continue
                    low, high = np.percentile(valid, [1, 99])
                    rgb[i] = np.clip((bands[i].filled(low)-low)/max(high-low, 1e-6)*255, 0, 255)
            return Image.fromarray(np.moveaxis(rgb, 0, -1), 'RGB')
