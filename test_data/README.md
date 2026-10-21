# Demo test: "Terrain of Izmir in one minute" (TIN)

The AI starts from an empty project and builds a small terrain map on its own: satellite basemap, a work area around
Kadifekale hill in Izmir, random points with heights, a TIN, contours, hillshade, styling and a PDF layout.
One scenario checks almost every part of AI Copilot.

The heights are **synthetic** (a modelled hill), not real measurements. Internet is needed only for the
satellite basemap and for finding "Kadifekale" by name.

## 1. Run the scenario with the AI

Open an **empty project**, open AI Copilot (**Vector > AI Copilot**) and send this request:

```
Open a satellite basemap and go to Kadifekale in Izmir. Create a 3 x 3 km work area around it in EPSG:32635,
place 150 random points inside it and give each point a synthetic height in a field "z": a hill of about 170 m
at the centre that falls towards the edges. Build a TIN from the points (Delaunay triangles and a TIN surface
with 10 m pixels), then 10 m contours with labels and a hillshade. Style the surface green - yellow - brown,
make an A4 layout titled "Terrain of Izmir (demo)" and export it to PDF. Print a short report with the numbers.
```

Weaker models can do the same step by step:

| Step | Request | Expected result |
|---|---|---|
| 1 | `Open a satellite basemap and go to Kadifekale in Izmir` | Esri satellite map centred on Kadifekale |
| 2 | `Create a 3 x 3 km work area around the map centre in EPSG:32635` | A square polygon layer, 3 x 3 km |
| 3 | `Place 150 random points inside the work area` | A point layer with 150 points |
| 4 | `Add a field "z" with a synthetic height: a 170 m hill at the centre falling to the edges` | Every point has a height, roughly 0 - 175 m |
| 5 | `Build Delaunay triangles from the points` | About 280 triangles |
| 6 | `Create a TIN surface from the field z with 10 m pixels` | A 300 x 300 px raster |
| 7 | `Make 10 m contours with labels and a hillshade` | About 15 - 18 contour levels, a hillshade raster |
| 8 | `Style the surface green - yellow - brown, make an A4 layout and export it to PDF` | A layout and a PDF file |

**What to look at:** the map shows a hill with dense contours on its slopes; the PDF has a title, legend,
scale bar, north arrow and a footer with the CRS and the basemap credit. The exact numbers depend on the random
points the AI chooses.

## 2. Reference check (without the AI)

`check_demo.py` runs the same scenario with plain PyQGIS and prints checks with real numbers. Use it to see the
expected result and to confirm that your QGIS version supports every step.

1. Open an empty project.
2. **Plugins > Python Console**, then **Show Editor**, **Open Script** and choose `check_demo.py`.
3. Press **Run Script**.

Result on QGIS 3.34 (the points are quasi-random, so the numbers are the same on every run):

```
OK    satellite basemap added  (Esri World Imagery)
OK    work area 3 x 3 km  (3.0 x 3.0 km, EPSG:32635)
OK    150 points inside the work area  (150 points)
OK    every point has a height  (z 0.0 - 174.3 m)
OK    TIN triangles (2n - 2 - h rule)  (284 triangles, expected 284)
OK    TIN surface passes through the points  (300 x 300 px, 142 points sampled, max difference 1.33 m)
OK    no empty cells inside the TIN  (382/382 samples)
OK    contours every 10 m  (17 levels, 10 - 170 m)
OK    hillshade created  (native:hillshade)
OK    PDF map exported  (958 KB)

RESULT: 10 of 10 checks passed.
```

The files (points, triangles, surface, contours, hillshade, PDF) are saved to `AI_Copilot_demo_tin` in your home
folder, in a new sub-folder for every run.

## What the checks mean

| Check | Why it matters |
|---|---|
| Triangles = 2n - 2 - h | A correct Delaunay triangulation of n points with h points on the outer edge always has this number of triangles |
| Surface passes through the points | A TIN surface must reproduce the measured heights (small differences come from the pixel size) |
| No empty cells inside the TIN | The surface has values everywhere inside the triangulated area |
| Contours every 10 m | Every contour level is a multiple of the interval |
