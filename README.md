# Cuenca Fácil — aplicación de escritorio local

Selecciona un GeoTIFF, marca el punto de vertido y calcula su cuenca. La aplicación busca otras teselas en la carpeta elegida, las incorpora si la cuenca llega a un borde y vuelve a calcular hasta que deja de crecer. No necesita QGIS, navegador, servidor ni Node.js para funcionar.

## Obtener `CuencaFacil.exe` sin instalar Python en tu ordenador

1. Crea un **repositorio privado** en GitHub y sube **todo el contenido de esta carpeta** a la raíz. Incluye `.github/workflows/windows.yml`; no subas los MDT al repositorio.
2. Abre **Actions → Construir aplicacion privada para Windows → Run workflow**.
3. Al terminar, descarga el artefacto **CuencaFacil-Windows**, descomprímelo y ejecuta `CuencaFacil.exe` dentro de la carpeta descargada. **Conserva toda la carpeta** junto al `.exe`.

El proceso empaqueta Python, las bibliotecas GIS y WhiteboxTools en el equipo Windows de GitHub. El uso posterior del programa es local. La construcción requiere que el servidor de WhiteboxTools esté disponible en ese momento. Si tu organización restringe GitHub Actions, puedes construirlo en un Windows con Python 3.12: descarga el ejecutable oficial `whitebox_tools.exe` a `bin/` y ejecuta `build_windows.bat`.

## Uso

1. Coloca las teselas originales `.tif`/`.tiff` en una carpeta local. Selecciona la carpeta y la tesela donde se encuentra el punto de vertido.
2. Marca el punto en el visor. Usa la rueda para acercarte. Comprueba que el punto queda en el cauce; el ajuste automático busca la mayor acumulación dentro del radio indicado (por defecto 12 m).
3. Escoge una carpeta de resultados **distinta de la carpeta de MDT** y pulsa **Delimitar cuenca**. El programa muestra en el registro cuándo añade teselas y dónde sitúa el punto ajustado.
4. Comprueba el estado **COMPLETA** o **PROVISIONAL**, el área y la posición ajustada. El estado provisional indica que la cuenca llega al límite de los datos disponibles o a una tesela incompatible. Añade las teselas que falten y recalcula.

Los MDT deben tener coordenadas horizontales en metros (por ejemplo ETRS89/UTM), misma proyección, resolución y retícula alineada. Las teselas incompatibles se señalan en `resultado.json` y no se remuestrean silenciosamente. El programa usa la primera banda. En caso de rasters con huecos NoData, estos se comprueban igual que los bordes. Los TIFF de salida previos deben quedar fuera de la carpeta de MDT.

## Método y resultados

El mosaico se escribe **por bloques en disco**. WhiteboxTools aplica `BreachDepressionsLeastCost` con relleno residual, dirección `D8Pointer`, acumulación en número de celdas y `Watershed`. La cuenca se delimita sobre el **punto ajustado**, no sobre la posición aproximada del clic. La búsqueda de brechas se controla con el número de celdas indicado (por defecto 50). La extensión se calcula contando celdas de la máscara final por la superficie de cada píxel, sin simplificar el borde.

La carpeta de resultados contiene:

| Archivo | Contenido |
| --- | --- |
| `cuenca.gpkg` | Polígono de la cuenca en el CRS original, listo para QGIS. |
| `cuenca_mascara.tif` | Máscara ráster georreferenciada (1 = cuenca). |
| `resultado.json` | Área, CRS, teselas utilizadas, posición elegida y ajustada, estado de cobertura y parámetros. |
| `pasada_01/`, `pasada_02/`… | Mosaico y resultados intermedios de cada ampliación. |

La metodología D8 y la corrección de depresiones son decisiones hidráulicas que pueden alterar divisorias, especialmente ante puentes, terraplenes, rellenos y vaguadas modificadas. El ajuste a máxima acumulación puede mover el vertido a otro ramal si el radio es excesivo. Comprueba el trazado sobre ortofoto y cartografía de cauces antes de emplearlo en un documento técnico. El estado **COMPLETA** confirma que la cuenca no toca huecos ni límites del conjunto de teselas disponible; no garantiza por sí mismo la calidad del MDT.

Para probar los cálculos antes de usar datos reales: `python smoke_test.py RUTA_A_whitebox_tools`. La prueba crea dos teselas sintéticas de 2 m, fuerza una ampliación y comprueba el área y los formatos exportados.
