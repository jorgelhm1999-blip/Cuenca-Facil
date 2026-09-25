"""Screen-sized previews of hydrological rasters; full grids remain on disk."""
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT


def diagnostic_preview(path: Path, kind: str, width: int, height: int) -> Image.Image:
    if kind not in ('direction', 'accumulation'):
        raise ValueError('Vista hidrológica desconocida.')
    with rasterio.open(path) as ds:
        if width < 1 or height < 1:
            raise ValueError('Dimensiones de vista inválidas.')
        if kind == 'direction':
            arr = ds.read(1, out_shape=(height, width), masked=True,
                          resampling=Resampling.nearest)
        else:
            transform = ds.transform * rasterio.Affine.scale(ds.width/width, ds.height/height)
            with WarpedVRT(ds, crs=ds.crs, transform=transform, width=width,
                           height=height, resampling=Resampling.max) as reduced:
                arr = reduced.read(1, masked=True)
    valid = ~np.ma.getmaskarray(arr)
    if kind == 'direction':
        # Whitebox D8 pointer codes: E, NE, N, NW, W, SW, S, SE.
        palette = {1: (63, 116, 194), 2: (54, 170, 199), 4: (73, 168, 116),
                   8: (144, 185, 90), 16: (235, 196, 79), 32: (240, 145, 73),
                   64: (202, 91, 121), 128: (139, 95, 180)}
        rgb = np.full((height, width, 3), 235, dtype='uint8')
        for code, color in palette.items():
            rgb[valid & (arr.data == code)] = color
    else:
        log_values = np.log1p(np.maximum(arr.filled(0), 0))
        upper = max(float(np.max(log_values[valid])) if valid.any() else 1, 1)
        t = np.clip(log_values / upper, 0, 1)
        rgb = np.zeros((height, width, 3), dtype='uint8')
        rgb[..., 0] = np.clip(25 + 234 * t**2, 0, 255).astype('uint8')
        rgb[..., 1] = np.clip(35 + 160 * t, 0, 255).astype('uint8')
        rgb[..., 2] = np.clip(55 + 155 * np.sqrt(t), 0, 255).astype('uint8')
        rgb[~valid] = (235, 235, 235)
    return Image.fromarray(rgb, 'RGB')
