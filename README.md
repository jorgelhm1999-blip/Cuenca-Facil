# Cuenca Fácil — aplicación de escritorio local

Selecciona un GeoTIFF, calcula y visualiza la red de drenaje, marca sobre ella el punto de vertido y delimita su cuenca. También puedes introducir directamente las coordenadas X/Y del punto en el CRS del MDT. La aplicación busca otras teselas en la carpeta elegida, las incorpora si la cuenca llega a un borde y vuelve a calcular hasta que deja de crecer. No necesita QGIS, navegador, servidor ni Node.js para funcionar.

## Obtener `CuencaFacil.exe` sin instalar Python en tu ordenador

1. Crea un **repositorio privado** en GitHub y sube **todo el contenido de esta carpeta** a la raíz. Incluye `.github/workflows/windows.yml`; no subas los MDT al repositorio.
2. Abre **Actions → Construir aplicacion privada para Windows → Run workflow**.
3. Al terminar, descarga el artefacto **CuencaFacil-Windows**, descomprímelo y ejecuta `CuencaFacil.exe` dentro de la carpeta descargada. **Conserva toda la carpeta** junto al `.exe`.

El proceso empaqueta Python, las bibliotecas GIS y WhiteboxTools en el equipo Windows de GitHub. El uso posterior del programa es local. La construcción requiere que el servidor de WhiteboxTools esté disponible en ese momento. Si tu organización restringe GitHub Actions, puedes construirlo en un Windows con Python 3.12: descarga el ejecutable oficial `whitebox_tools.exe` a `bin/` y ejecuta `build_windows.bat`.

## Uso

1. Coloca las teselas originales `.tif`/`.tiff` en una carpeta local. Selecciona la carpeta y la tesela donde se encuentra el punto de vertido. La vista intenta cargar automáticamente la ortofoto PNOA por Internet. También puedes escoger una ortofoto GeoTIFF RGB local. La barra «Transparencia de ortofoto» comienza en 0 % (visible) y permite ver gradualmente el MDT; al 100 % se ve únicamente el MDT. El cálculo hidrológico se ejecuta localmente.
2. Elige una carpeta de resultados **distinta de la carpeta de MDT** y pulsa **Calcular y mostrar cauces**. La aplicación dibuja en azul las celdas con al menos el área aportante indicada (por defecto 0,05 km²). Puedes bajar el umbral y pulsar **Actualizar umbral de cauces** si no aparecen cauces. Esta red **preliminar** solo utiliza la tesela inicial: la aportación real puede aumentar cuando se añadan teselas aguas arriba.
3. Acerca el visor y haz clic sobre el cauce deseado. También puedes escribir sus coordenadas X/Y en el CRS del MDT y pulsar **Situar punto por coordenadas**. Antes de calcular la cuenca verás en cian la celda de cauce más próxima dentro del radio (por defecto 6 m), el desplazamiento y su aportación preliminar. Si no hay cauce dentro del radio, modifica el punto, el umbral o el radio. Si conoces coordenadas exactas, puedes introducirlas y delimitar sin calcular antes los cauces.
4. Pulsa **Delimitar cuenca**. El programa conserva la celda de vertido confirmada, reutiliza los cálculos previos de la tesela inicial y te avisa si debe incorporar más teselas.
5. Comprueba el estado **COMPLETA** o **PROVISIONAL**, el área y la posición ajustada. El estado provisional indica que la cuenca llega al límite de los datos disponibles o a una tesela incompatible. Añade las teselas que falten y recalcula.
6. Si la cuenca de referencia invade otra hoja, escoge esa hoja en «Comprobar teselas vecinas» y pulsa **Auditar y comparar**. Puedes elegir «Todas las teselas vecinas» si procede; esta opción puede requerir más tiempo y memoria. La auditoría conserva el mismo punto de vertido, repite el análisis incluyendo las teselas indicadas y guarda otra cuenca y un ráster de diferencias. En `diferencias_auditoria.tif`, 1 significa **solo en la cuenca original**, 2 significa **solo en la auditada** y 0 significa **sin diferencia**. `comparacion.json` informa las superficies ganadas y perdidas y el porcentaje de coincidencia espacial. Si aparecen diferencias en el límite de la hoja omitida, la selección automática de teselas influyó en la primera delimitación. Si persisten discrepancias con QGIS, revisa el punto, el tratamiento de depresiones, el modelo de flujo y el MDT empleado.

Los MDT deben tener coordenadas horizontales en metros (por ejemplo ETRS89/UTM), misma proyección, resolución y retícula alineada. Las teselas incompatibles se señalan en `resultado.json` y no se remuestrean silenciosamente. El programa usa la primera banda. En caso de rasters con huecos NoData, estos se comprueban igual que los bordes. Los TIFF de salida previos deben quedar fuera de la carpeta de MDT.

## Método y resultados

El mosaico se escribe **por bloques en disco**. WhiteboxTools aplica `BreachDepressionsLeastCost` con relleno residual, dirección `D8Pointer`, acumulación en número de celdas y `Watershed`. Al pulsar sobre el cauce calculado se escoge la **celda más cercana** que supera el umbral, evitando que el vertido se desplace aguas abajo buscando la máxima acumulación. La celda elegida permanece fija cuando se amplía el mosaico. Sin el cálculo previo de cauces se conserva el ajuste tradicional por máxima acumulación. La búsqueda de brechas se controla con el número de celdas indicado (por defecto 50). La extensión se calcula contando celdas de la máscara final por la superficie de cada píxel, sin simplificar el borde.

La carpeta de resultados contiene:

| Archivo | Contenido |
| --- | --- |
| `cuenca.gpkg` | Polígono de la cuenca en el CRS original, listo para QGIS. |
| `cuenca_mascara.tif` | Máscara ráster georreferenciada (1 = cuenca). |
| `resultado.json` | Área, CRS, teselas utilizadas, posición elegida y ajustada, estado de cobertura y parámetros. |
| `preparacion/` | Mosaico, MDT corregido, dirección D8 y acumulación de la tesela inicial; permite volver a probar otros puntos sin repetirlos. |
| `pasada_01/`, `pasada_02/`… | Mosaico y resultados intermedios de cada ampliación. |

La metodología D8 y la corrección de depresiones son decisiones hidráulicas que pueden alterar divisorias, especialmente ante puentes, terraplenes, rellenos y vaguadas modificadas. El ajuste a máxima acumulación puede mover el vertido a otro ramal si el radio es excesivo. Comprueba el trazado sobre ortofoto y cartografía de cauces antes de emplearlo en un documento técnico. El estado **COMPLETA** confirma que la cuenca no toca huecos ni límites del conjunto de teselas disponible; no garantiza por sí mismo la calidad del MDT.

La capa «Drainage Basins» de SAGA/QGIS divide la superficie en cuencas de drenaje y permite una comparación visual de divisorias. Cuenca Fácil **no la usa para recortar ni forzar la cuenca**: calcula el área aportante específica al punto de vertido con `Watershed` de WhiteboxTools. Un área total parecida a la de QGIS no demuestra por sí sola que las dos cuencas ocupen las mismas celdas. La selección automática incorpora nuevas hojas al detectar que su propia cuenca llega a un borde; para comprobar una hoja vecina que no detectó se usa la auditoría del paso 6.

Para probar los cálculos antes de usar datos reales: `python smoke_test.py RUTA_A_whitebox_tools`. La prueba crea dos teselas sintéticas de 2 m, dibuja y reutiliza la red previa, fuerza una ampliación y comprueba el área y los formatos exportados.

## Si falla un cálculo

El campo «Motor hidrológico» se deja vacío con la versión de Windows descargada: el programa encuentra automáticamente el ejecutable incluido. Escoge una carpeta de resultados local y con suficiente espacio libre, separada de las teselas de entrada. Cada pasada guarda un `registro_motor.txt` con la salida íntegra y el código de error. Si aparece un error al «Saving data...», conserva el registro de `pasada_01` y la dimensión en píxeles de la tesela; esos datos permiten distinguir falta de espacio, un fallo de WhiteboxTools o un límite del archivo. No consideres válido un resultado parcial.
