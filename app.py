"""Small offline desktop GUI for guided watershed delineation."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import math
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import rasterio
from PIL import Image, ImageFilter, ImageTk
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from engine import delineate, index_tiles, locate_whitebox, prepare_hydrology, snap_to_stream


class Window(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Cuenca Fácil · Delimitación de cuencas')
        self.geometry('1250x860')
        self.minsize(980, 730)
        self.tiles = []
        self.preview = None
        self.preview_bounds = None
        self.overlay = None
        self.stream_overlay = None
        self.accum_path = None
        self.outlet = None
        self.result_outlet = None
        self.zoom = 1.0
        self.pan_x = self.pan_y = 0.0
        self.drag_start = None
        self.image_ref = None
        self.events = queue.Queue()
        self.busy = False
        self.folder = tk.StringVar()
        self.output = tk.StringVar()
        self.binary = tk.StringVar()
        self.radius = tk.StringVar(value='6')
        self.breach = tk.StringVar(value='50')
        self.selection = tk.StringVar()
        self.x_coord = tk.StringVar()
        self.y_coord = tk.StringVar()
        self.threshold = tk.StringVar(value='0,05')
        self._layout()
        self.after(120, self._poll)

    def _layout(self):
        left = ttk.Frame(self, padding=14)
        left.pack(side='left', fill='y')
        left.configure(width=335)
        left.pack_propagate(False)
        ttk.Label(left, text='CUENCA FÁCIL', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(left, text='MDT local · Cauces y cuenca desde un vertido',
                  wraplength=300).pack(anchor='w', pady=(1, 6))
        self._heading(left, '1  Carpeta y tesela inicial')
        ttk.Entry(left, textvariable=self.folder).pack(fill='x', pady=(3, 3))
        ttk.Button(left, text='Elegir carpeta de MDT', command=self.pick_folder).pack(fill='x')
        self.tile_box = ttk.Combobox(left, state='readonly', textvariable=self.selection)
        self.tile_box.pack(fill='x', pady=(9, 2))
        self.tile_box.bind('<<ComboboxSelected>>', lambda e: self.load_preview())
        ttk.Label(left, text='Solo TIFF con retícula compatible y CRS en metros.',
                  wraplength=300, foreground='#586575').pack(anchor='w')
        self._heading(left, '2  Mostrar cauces calculados')
        threshold_line = ttk.Frame(left)
        threshold_line.pack(fill='x', pady=(5, 4))
        ttk.Label(threshold_line, text='Área mínima drenada (km²)').pack(side='left')
        ttk.Spinbox(threshold_line, from_=0.0001, to=1000, increment=0.01, width=9,
                    textvariable=self.threshold).pack(side='right')
        self.network_button = ttk.Button(left, text='Calcular y mostrar cauces', command=self.prepare)
        self.network_button.pack(fill='x', pady=(1, 3))
        ttk.Button(left, text='Actualizar umbral de cauces',
                   command=self.refresh_stream_overlay).pack(fill='x')
        ttk.Label(left, text='La red inicial usa esta tesela; al delimitar se incorporan las vecinas necesarias.',
                  wraplength=300, foreground='#586575').pack(anchor='w', pady=(3, 0))
        self._heading(left, '3  Punto de vertido')
        ttk.Label(left, text='Haz clic sobre un cauce azul o introduce coordenadas del CRS del MDT.',
                  wraplength=300).pack(anchor='w', pady=(3, 3))
        coords = ttk.Frame(left)
        coords.pack(fill='x', pady=(2, 3))
        ttk.Label(coords, text='X').grid(row=0, column=0)
        ttk.Entry(coords, textvariable=self.x_coord, width=11).grid(row=0, column=1, padx=(2, 8))
        ttk.Label(coords, text='Y').grid(row=0, column=2)
        ttk.Entry(coords, textvariable=self.y_coord, width=11).grid(row=0, column=3, padx=(2, 0))
        ttk.Button(left, text='Situar punto por coordenadas', command=self.go_coordinates).pack(fill='x')
        self.point_label = ttk.Label(left, text='Sin seleccionar', foreground='#657280')
        self.point_label.pack(anchor='w', pady=(3, 0))
        line = ttk.Frame(left)
        line.pack(fill='x', pady=(8, 0))
        ttk.Label(line, text='Radio de ajuste (m)').pack(side='left')
        ttk.Spinbox(line, from_=0, to=1000, increment=2, width=8,
                    textvariable=self.radius).pack(side='right')
        line2 = ttk.Frame(left)
        line2.pack(fill='x', pady=(5, 0))
        ttk.Label(line2, text='Búsqueda de brecha (celdas)').pack(side='left')
        ttk.Spinbox(line2, from_=1, to=1000, increment=10, width=8,
                    textvariable=self.breach).pack(side='right')
        self._heading(left, '4  Calcular y guardar')
        ttk.Entry(left, textvariable=self.output).pack(fill='x', pady=(4, 3))
        ttk.Button(left, text='Elegir carpeta de resultados', command=self.pick_output).pack(fill='x')
        self.calculate = ttk.Button(left, text='Delimitar cuenca', command=self.start)
        self.calculate.pack(fill='x', pady=(12, 5), ipady=7)
        self.status = tk.StringVar(value='Elige una carpeta con teselas GeoTIFF.')
        ttk.Label(left, textvariable=self.status, wraplength=300,
                  foreground='#11645d').pack(anchor='w', pady=(3, 0))
        ttk.Separator(left).pack(fill='x', pady=13)
        ttk.Label(left, text='Motor hidrológico (WhiteboxTools)',
                  font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        ttk.Entry(left, textvariable=self.binary).pack(fill='x', pady=(4, 2))
        ttk.Button(left, text='Localizar ejecutable…', command=self.pick_binary).pack(fill='x')
        ttk.Label(left, text='Se detecta automáticamente en la versión descargada. Déjalo vacío.',
                  wraplength=300, foreground='#586575').pack(anchor='w', pady=(3, 0))

        right = ttk.Frame(self, padding=(2, 14, 14, 10))
        right.pack(side='right', expand=True, fill='both')
        ttk.Label(right, text='Vista del MDT', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(right, text='Azul: drenaje del MDT · Rueda: acercar · Arrastrar: mover · Clic: vertido',
                  foreground='#586575').pack(anchor='w', pady=(0, 5))
        self.canvas = tk.Canvas(right, background='#e6eaec', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(expand=True, fill='both')
        self.canvas.bind('<Configure>', lambda e: self.draw())
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Button-4>', lambda e: self.change_zoom(1.2, e.x, e.y))
        self.canvas.bind('<Button-5>', lambda e: self.change_zoom(1/1.2, e.x, e.y))
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        ttk.Label(right, text='Proceso', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(10, 3))
        self.log = tk.Text(right, height=7, state='disabled', wrap='word', font=('Consolas', 9))
        self.log.pack(fill='x')

    @staticmethod
    def _heading(parent, title):
        ttk.Label(parent, text=title, font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14, 0))

    def _log(self, line):
        self.log.configure(state='normal')
        self.log.insert('end', line + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def pick_folder(self):
        path = filedialog.askdirectory(title='Carpeta con MDT')
        if not path: return
        self.folder.set(path)
        self.tiles, errors = index_tiles(Path(path))
        self.tile_box['values'] = [str(t.path.relative_to(Path(path))) for t in self.tiles]
        self._log(f'{len(self.tiles)} teselas indexadas en {path}.')
        for err in errors[:10]: self._log('Se omite ' + err)
        if self.tiles:
            self.tile_box.current(0)
            self.load_preview()
        else:
            messagebox.showwarning('Sin MDT', 'No hay GeoTIFF válidos con coordenadas en metros.')

    def pick_output(self):
        path = filedialog.askdirectory(title='Carpeta de resultados')
        if path: self.output.set(path)

    def pick_binary(self):
        path = filedialog.askopenfilename(title='Ejecutable whitebox_tools')
        if path: self.binary.set(path)

    def load_preview(self):
        if not self.tiles or self.tile_box.current() < 0: return
        tile = self.tiles[self.tile_box.current()]
        try:
            with rasterio.open(tile.path) as ds:
                factor = max(ds.width / 1200, ds.height / 1200, 1)
                height, width = max(1, round(ds.height/factor)), max(1, round(ds.width/factor))
                arr = ds.read(1, out_shape=(height, width), resampling=Resampling.average, masked=True)
                valid = arr.compressed()
                if len(valid) == 0: raise ValueError('El MDT no contiene valores válidos.')
                low, high = np.percentile(valid, [2, 98])
                grey = np.clip((arr.filled(low)-low) / max(high-low, 1e-6)*255, 0, 255).astype('uint8')
                grey[np.ma.getmaskarray(arr)] = 230
            self.preview = Image.fromarray(grey, 'L').convert('RGB')
            self.preview_bounds = tuple(tile.bounds)
            self.overlay = None
            self.stream_overlay = None
            self.accum_path = None
            self.outlet = self.result_outlet = None
            self.x_coord.set('')
            self.y_coord.set('')
            self.zoom = 1
            self.pan_x = self.pan_y = 0
            self.point_label.configure(text='Sin seleccionar')
            self.status.set(f'{tile.path.name} · {tile.resolution[0]:g} × {tile.resolution[1]:g} m · {tile.crs}')
            self.draw()
        except Exception as exc:
            messagebox.showerror('No se pudo abrir el MDT', str(exc))

    def load_result_preview(self, destination: Path, iteration: int):
        mosaic = destination / f'pasada_{iteration:02d}' / 'mosaico.tif'
        with rasterio.open(mosaic) as ds:
            factor = max(ds.width / 1200, ds.height / 1200, 1)
            height, width = max(1, round(ds.height/factor)), max(1, round(ds.width/factor))
            arr = ds.read(1, out_shape=(height, width),
                          resampling=Resampling.average, masked=True)
            valid = arr.compressed()
            low, high = np.percentile(valid, [2, 98])
            grey = np.clip((arr.filled(low)-low)/max(high-low, 1e-6)*255, 0, 255).astype('uint8')
            grey[np.ma.getmaskarray(arr)] = 230
            self.preview = Image.fromarray(grey, 'L').convert('RGB')
            self.preview_bounds = tuple(ds.bounds)
        with rasterio.open(destination/'cuenca_mascara.tif') as src:
            mask = src.read(1, out_shape=(height, width), resampling=Resampling.nearest)
        color = np.zeros((height, width, 4), dtype='uint8')
        color[mask == 1] = (0, 218, 190, 118)
        self.overlay = Image.fromarray(color, 'RGBA')
        self.stream_overlay = None
        self.zoom = 1
        self.pan_x = self.pan_y = 0

    def _layout_image(self):
        cw, ch = max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)
        if self.preview is None: return None
        w, h = self.preview.size
        scale = min(cw/w, ch/h) * self.zoom
        return (cw-w*scale)/2+self.pan_x, (ch-h*scale)/2+self.pan_y, scale

    def draw(self):
        self.canvas.delete('all')
        layout = self._layout_image()
        if not layout:
            self.canvas.create_text(200, 160, text='Selecciona una carpeta y un MDT', fill='#5b6b76')
            return
        x0, y0, scale = layout
        image = self.preview.copy()
        if self.stream_overlay is not None and self.stream_overlay.size == image.size:
            image = Image.alpha_composite(image.convert('RGBA'), self.stream_overlay).convert('RGB')
        if self.overlay is not None and self.overlay.size == image.size:
            image = Image.alpha_composite(image.convert('RGBA'), self.overlay).convert('RGB')
        self.image_ref = ImageTk.PhotoImage(image.resize(
            (max(1, round(image.width*scale)), max(1, round(image.height*scale))),
            Image.Resampling.BILINEAR))
        self.canvas.create_image(x0, y0, image=self.image_ref, anchor='nw')
        for position, color, label in ((self.outlet, '#fbc02d', 'Punto elegido'),
                                       (self.result_outlet, '#00dfd4', 'Ajustado')):
            if position:
                left, bottom, right, top = self.preview_bounds
                px = x0 + (position[0]-left)/(right-left)*image.width*scale
                py = y0 + (top-position[1])/(top-bottom)*image.height*scale
                self.canvas.create_oval(px-6, py-6, px+6, py+6, fill=color,
                                        outline='#222', width=2)
                self.canvas.create_text(px+9, py-12, anchor='w', text=label, fill='#111')

    def change_zoom(self, mult, x, y):
        if self.preview is None: return
        new_zoom = max(1, min(20, self.zoom*mult))
        ratio = new_zoom/self.zoom
        self.zoom = new_zoom
        self.pan_x = ratio*self.pan_x + (1-ratio)*(x-self.canvas.winfo_width()/2)
        self.pan_y = ratio*self.pan_y + (1-ratio)*(y-self.canvas.winfo_height()/2)
        self.draw()

    def wheel(self, e): self.change_zoom(1.2 if e.delta > 0 else 1/1.2, e.x, e.y)
    def press(self, e): self.drag_start = (e.x, e.y, False)
    def drag(self, e):
        if self.drag_start:
            x, y, _ = self.drag_start
            self.pan_x += e.x-x
            self.pan_y += e.y-y
            self.drag_start = (e.x, e.y, True)
            self.draw()

    def release(self, e):
        if not self.drag_start: return
        dragged = self.drag_start[2]
        self.drag_start = None
        if dragged or not self.preview: return
        x0, y0, scale = self._layout_image()
        px, py = (e.x-x0)/scale, (e.y-y0)/scale
        if not (0 <= px < self.preview.width and 0 <= py < self.preview.height): return
        left, bottom, right, top = self.preview_bounds
        self.set_point((left + px/self.preview.width*(right-left),
                        top - py/self.preview.height*(top-bottom)))

    def set_point(self, point):
        self.outlet = point
        self.x_coord.set(f'{point[0]:.2f}')
        self.y_coord.set(f'{point[1]:.2f}')
        self.result_outlet = None
        self.overlay = None
        info = f'X {point[0]:.2f} · Y {point[1]:.2f}'
        if self.accum_path:
            try:
                radius = float(self.radius.get().replace(',', '.'))
                threshold = float(self.threshold.get().replace(',', '.'))
                snapped, cells, _ = snap_to_stream(self.accum_path, point, radius, threshold)
                self.result_outlet = snapped
                pixel_area = self.tiles[self.tile_box.current()].resolution
                area = cells*pixel_area[0]*pixel_area[1]/1_000_000
                info += f'\nAjuste {math.dist(point, snapped):.1f} m · aporte previo {area:.4f} km²'
            except (ValueError, OSError) as exc:
                info += '\nSin ajuste: ' + str(exc)
        self.point_label.configure(text=info)
        self.draw()

    def go_coordinates(self):
        try:
            point = (float(self.x_coord.get().replace(',', '.')),
                     float(self.y_coord.get().replace(',', '.')))
        except ValueError:
            messagebox.showerror('Coordenadas', 'Introduce X e Y numéricas en el CRS del MDT.')
            return
        matches = [i for i, tile in enumerate(self.tiles) if tile.bounds[0] <= point[0] < tile.bounds[2]
                   and tile.bounds[1] < point[1] <= tile.bounds[3]]
        if not matches:
            messagebox.showerror('Coordenadas', 'Ese punto no está dentro de ninguna tesela indexada.')
            return
        if matches[0] != self.tile_box.current():
            self.tile_box.current(matches[0])
            self.load_preview()
        self.set_point(point)

    def refresh_stream_overlay(self):
        if not self.accum_path or not self.preview:
            self._log('Calcula primero los cauces de la tesela seleccionada.')
            return
        try:
            threshold = float(self.threshold.get().replace(',', '.'))
            if threshold <= 0: raise ValueError('El área mínima debe ser positiva.')
            with rasterio.open(self.accum_path) as ds:
                h, w = self.preview.height, self.preview.width
                cells = threshold*1_000_000/abs(ds.transform.a*ds.transform.e)
                transform = ds.transform * rasterio.Affine.scale(ds.width/w, ds.height/h)
                with WarpedVRT(ds, crs=ds.crs, transform=transform, width=w,
                               height=h, resampling=Resampling.max) as reduced:
                    data = reduced.read(1, masked=True)
                network = (data.data >= cells) & (~np.ma.getmaskarray(data))
            bright = Image.fromarray((network*255).astype('uint8'), 'L').filter(ImageFilter.MaxFilter(3))
            alpha = np.asarray(bright)
            color = np.zeros((h, w, 4), dtype='uint8')
            color[:, :, 0] = 0
            color[:, :, 1] = 229
            color[:, :, 2] = 255
            color[:, :, 3] = alpha
            self.stream_overlay = Image.fromarray(color, 'RGBA')
            self.draw()
            self._log(f'Cauces visibles: umbral {threshold:g} km². Aporte preliminar limitado a la tesela inicial.')
            if self.outlet: self.set_point(self.outlet)
            if not np.any(network):
                self._log('No aparecen cauces: reduce el área mínima drenada y pulsa Actualizar.')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Umbral de cauces', str(exc))

    def prepare(self):
        if self.busy or self.tile_box.current() < 0: return
        if not self.output.get():
            self.pick_output()
            if not self.output.get(): return
        try:
            exe = locate_whitebox(self.binary.get() or None)
            breach = int(self.breach.get())
            if breach < 1: raise ValueError('La búsqueda de brecha debe ser positiva.')
        except (ValueError, FileNotFoundError) as exc:
            messagebox.showerror('Parámetros', str(exc))
            return
        self.busy = True
        self.calculate.configure(state='disabled')
        self.network_button.configure(state='disabled')
        self.status.set('Calculando dirección y acumulación de flujo…')
        tile = self.tiles[self.tile_box.current()]
        destination = Path(self.output.get())
        def worker():
            try:
                path = prepare_hydrology(tile, destination, exe, breach,
                                         log=lambda line: self.events.put(('log', line)))
                self.events.put(('network_ready', path))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def start(self):
        if self.busy: return
        if self.accum_path and self.outlet:
            self.set_point(self.outlet)
        if not self.outlet or self.tile_box.current() < 0:
            messagebox.showwarning('Falta el punto', 'Selecciona una tesela y haz clic en el punto de vertido.')
            return
        if self.accum_path and not self.result_outlet:
            messagebox.showwarning('Punto fuera del cauce',
                                   'Elige un cauce azul próximo o modifica el radio y vuelve a señalar el punto.')
            return
        if not self.output.get():
            self.pick_output()
            if not self.output.get(): return
        try:
            radius, breach = float(self.radius.get().replace(',', '.')), int(self.breach.get())
            exe = locate_whitebox(self.binary.get() or None)
            if radius < 0 or breach < 1: raise ValueError('Parámetros fuera de rango')
        except (ValueError, FileNotFoundError) as exc:
            messagebox.showerror('Parámetros', str(exc))
            return
        self.busy = True
        self.calculate.configure(state='disabled')
        self.network_button.configure(state='disabled')
        self.status.set('Calculando. Puede tardar según el tamaño del MDT…')
        point, confirmed = self.outlet, self.result_outlet
        destination = Path(self.output.get())
        candidates = [tile for tile in self.tiles if tile.bounds[0] <= point[0] < tile.bounds[2]
                      and tile.bounds[1] < point[1] <= tile.bounds[3]]
        if not candidates:
            self.busy = False
            self.calculate.configure(state='normal')
            self.network_button.configure(state='normal')
            messagebox.showerror('Punto fuera del MDT', 'El punto no coincide con una tesela indexada.')
            return
        tile = candidates[0]
        def worker():
            try:
                result = delineate(self.tiles, tile.path, point, destination, exe, radius,
                                   breach, log=lambda line: self.events.put(('log', line)),
                                   fixed_point=confirmed)
                self.events.put(('done', (result, destination)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log': self._log(value)
                elif kind == 'error':
                    self.busy = False
                    self.calculate.configure(state='normal')
                    self.network_button.configure(state='normal')
                    self.status.set('No se pudo completar el cálculo.')
                    messagebox.showerror('Error de cálculo', value)
                elif kind == 'network_ready':
                    self.busy = False
                    self.calculate.configure(state='normal')
                    self.network_button.configure(state='normal')
                    self.accum_path = value
                    self.refresh_stream_overlay()
                    if self.outlet: self.set_point(self.outlet)
                    self.status.set('Cauces calculados. Elige el vertido sobre una línea azul.')
                elif kind == 'done':
                    self.busy = False
                    self.calculate.configure(state='normal')
                    self.network_button.configure(state='normal')
                    result, destination = value
                    self.result_outlet = tuple(result['punto_ajustado'])
                    try:
                        self.load_result_preview(destination, result['iteraciones'])
                        self.accum_path = Path(result['directorio_final'])/'acumulacion_celdas.tif'
                        self.refresh_stream_overlay()
                    except Exception as exc:
                        self._log('No se pudo mostrar la máscara en el visor: ' + str(exc))
                    self.status.set(f'Área: {result["area_km2"]:.4f} km² · '
                                    + ('COMPLETA' if result['resultado_completo'] else 'PROVISIONAL'))
                    self.draw()
                    messagebox.showinfo('Cuenca delimitada',
                        f'Área: {result["area_km2"]:.4f} km²\n'
                        f'Teselas usadas: {len(result["teselas"])}\n'
                        f'Estado: {"completa" if result["resultado_completo"] else "PROVISIONAL; faltan datos en un borde"}\n\n'
                        f'Resultados: {destination}')
        except queue.Empty: pass
        self.after(120, self._poll)


if __name__ == '__main__':
    Window().mainloop()
