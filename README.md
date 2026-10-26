# AI Copilot for QGIS

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![QGIS Version](https://img.shields.io/badge/QGIS-3.28%2B%20%7C%204.x-brightgreen.svg)](https://qgis.org)

**AI Copilot** is a universal artificial intelligence assistant plugin for QGIS. It translates natural language instructions into working PyQGIS code and executes spatial workflows directly on your canvas. 

It now features a **full chat interface**, allowing the AI to explain its spatial logic before safely executing the underlying code.

---

## Key Features

### New in 1.8 — your own commands
- **`/newcommand <name> <what it does>`** — the AI writes the command, you check it in the editor and press *Save*; after that it runs instantly, without the AI. Example: `/newcommand slope slope in degrees of a DEM layer, coloured`.
- **`/commands`** — editor of your commands: new, edit, delete, import / export (share them with colleagues). Two kinds:
  - *List of commands* — no programming: one command per line, run as one action (one Undo).
    `$1` … `$9` are your arguments, `${2:100}` = argument 2 or 100, `$*` = all arguments, `$last` = the layer the previous line created:
    ```
    /measure $1
    /calc $1 density = round("$2" / "area_km2", 1)
    /style $1 density
    ```
    saved as `density`, it runs as `/density Districts pop`.
  - *Python* — the same environment as AI code; `args`, `kwargs` and `arg(1, default)` hold the arguments.
- **Scenarios → Make a command…** turns a saved task into a command.
- Four examples are added on the first start: `/relief`, `/density`, `/quickmap`, `/osmhex`. Your commands are listed in `/help` and shown while you type `/`.

### New in 1.7
- **Tidy panel:** provider, model, API key and the options are folded under **Settings**; a one-line summary shows the model (red if the key is missing). Chat, buttons and the request bar stay in view.
- **Russian interface:** *Settings → Interface language*: Auto (follows the QGIS language; Russian when QGIS is in Russian) or English. Switches at once, without a restart.
- **Risky code check:** before code that deletes or moves files, runs system commands or hidden code, clears the project or writes straight into the data of an existing layer (SHP / GPKG changes that Undo AI cannot revert) the plugin lists what the code will do and asks — also with Auto-run. Switch: *Ask before risky code*.
- **Compact chat:** the AI's explanation is shown, its code is folded behind **▸ Show code**; long output and errors show a few lines with **Show all**.
- **More quick commands:** `/info`, `/stats`, `/count`, `/top`, `/calc`, `/measure`, `/heatmap`, `/hexbin`, `/nearest`, `/intersect`, `/merge`, `/fix`, `/csv`, `/pdf`, `/screenshot` — see `/help`.

### New in 1.6
- **Two dockable panels:** the request bar is docked at the bottom centre of the QGIS window (input box, **Run with AI**, live status). Settings and chat are a second panel, docked on the right by default; drag it to any side or float it — its place is remembered. The toolbar button shows / hides both.
- **Undo AI:** one click reverts the last AI action — layers it added are removed, layers it removed come back (temporary layers with their data), styles, labels, filters, names, visibility and table joins are restored, data edits are reverted, new layouts are removed and the map view returns. The last 10 actions can be undone one by one. Files written to disk are not deleted.
- **Field value hints:** the AI sees the real ranges and values of the fields of the working layers (e.g. `type: 'farm'|'forest'|'urban'`, `pop: 12..53400`) and writes filters with exactly those values. `/fields` opens a window that shows layers → fields → values; a double click puts a field or a value into your request. Switch off with *Field value hints for the AI*.
- **Smart join:** `/join` (or just ask the AI) joins a CSV / Excel table to a map layer. The key fields are found by comparing the values, and `"01"` = `1`, `İzmir` = `IZMIR`, `Bayraklı` = `Bayrakli` still match. Exact keys → live QGIS join; keys that only match after normalising → a new layer with the table columns. It reports how many features got data and which table rows did not match.
- **No freezing:** Processing algorithms (from the AI, commands and `run_alg`) run in a background thread; the map, the panel and **Stop** stay responsive. `run_in_background(func)` does the same for slow Python computations.
- **Saved scenarios:** **Scenarios…** saves a task together with the code that worked. Run it again later *without the AI* (instant, free, the same result), send it to the AI again, or share it via export / import.
- **Commands instead of code:** type `/` in the request box for quick commands that run at once without the AI: `/buffer Roads 250`, `/clip Parcels Boundary`, `/alg native:centroids INPUT=Parcels`, `/filter Parcels "area" > 5000`, `/zoom Izmir`, `/join`, `/fields`, `/undo`, `/scenario <name>`, `/help` for the full list. The AI itself now prefers these validated one-line commands to long raw PyQGIS code.
- **Request box:** a clean modern font (Segoe UI / Helvetica Neue / Ubuntu / Noto Sans, whatever your system has), a bit smaller, rounded border; **Ctrl+Enter** sends.

- **Fast answers:** the *Fast answers* option (on by default) limits the internal "thinking" of models that support it (Gemini 2.5 / 3, OpenAI o3, o4, gpt-5), so answers come much faster. Switch it off for very complex tasks.
- **Request archive and memory:** every request is saved with the answer and the result (**History** button: search, view, send again). The AI remembers the conversation (the last 8 messages), also after a QGIS restart (if it was used in the last 12 hours); **Clear** starts a new conversation.
- **Smart data links:** open the *Data link* section, paste a link and ask your question. Only that page is searched: other pages and other websites are never opened (links, files and services of other websites are ignored; OpenStreetMap only if you ask for it). The plugin reads the page in the background (QGIS does not freeze) and gives the AI the whole text of the page in its order, its headings, tables and links together with your question, so the AI takes the data directly from the text - also from lines and lists like "Konak: 336.545", not only from tables. Data files and map services that the page links to can be loaded too.
- **Answers in your language:** ask in Russian, Turkish, English or any other language and the AI answers in the same language (also in multi-step agent tasks and fixes). Transliterated text, e.g. Russian typed in Latin letters, is recognised too. The plugin interface itself is in English.
- **Natural Language Geoprocessing:** Describe what you want to achieve (buffering, layer intersections, filtering, spatial statistics), and the AI writes and executes the code.
- **Interactive Chat Interface:** The AI acts as a true assistant. It answers theoretical GIS questions, explains its algorithm in plain text, and separates the executable code cleanly for the parser.
- **"God Mode" Execution Scope:** Forget about `NameError`! The plugin automatically pre-loads the entire `qgis.core` library (3000+ classes) into the execution memory. The AI can use any QGIS class without writing a single `import` statement.
- **Bulletproof Qt6 / QGIS 4 Compatibility:** Built-in regex filters actively catch and replace outdated `PyQt5` imports generated by the AI, seamlessly converting them to `qgis.PyQt` to prevent plugin crashes.
- **Only what you ask:** the AI avoids unrequested actions. OpenStreetMap is downloaded only when your request mentions OSM/OpenStreetMap; otherwise it asks first, and a "No" is respected for the whole task.
- **Layers for AI:** choose which layers the AI works with — all, selected in the Layers panel, visible, or a chosen list. Those layers are sent with fields and details, the rest by name only, so large projects use far fewer tokens. The chosen list is saved in the project.
- **Project Context Awareness:** Automatically reads project CRS, active layers, attribute schema, and geometry types to avoid coordinate system mismatches.
- **Works with your other plugins:** the AI sees every installed plugin together with what can be used from it: its Processing algorithms, menu items and toolbar buttons. `plugin("name")` prints a plugin's algorithms, actions and Python API; a disabled plugin is enabled after your confirmation (including its Processing algorithms), a missing one opens the plugin manager.
- **Data from the internet only with your link:** paste a link to a website, file or API into the **Data link** field (or into the request). The AI reads it, finds the data, lets you choose, shows a sample and asks before using it. Without a link the AI does not access or suggest websites (OpenStreetMap download is built in).
- **Layouts and export:** the AI builds a print layout (map, title, legend, scale bar, north arrow, sources) and exports layouts, layers, rasters, map images and the project to any common format.
- **Live status panel:** always shows what is happening — connecting, the model thinking, writing the answer or code, running code, downloading data — with elapsed time and real progress in %. **Stop** cancels the request or the running code.
- **Agent mode:** for complex tasks the AI first inspects algorithms or data, then continues automatically, and asks for a fix when code fails (up to 6 steps). With Auto-run off you still confirm every step.
- **Execution feedback:** Everything the code prints is shown in the chat and sent back to the AI, so it can first inspect an unfamiliar plugin and then act. If code fails, press **Fix with AI**.
- **Multi-Provider Support:**
  - Ollama (local models)
  - Google Gemini
  - OpenAI
  - DeepSeek
  - Anthropic Claude

  The model list is loaded automatically from the provider once a valid key is entered.
- **Persistent Key Storage:** API keys are saved locally in the QGIS user profile (`QgsSettings`, plain text — do not share your profile folder).
- **Local AI:** Ollama models run fully offline, no key needed.

---

## Installation

### Method 1: Official QGIS Plugin Repository
1. In QGIS, navigate to **Plugins** -> **Manage and Install Plugins...**
2. Search for `AI Copilot`.
3. Check the box to enable it.

### Method 2: Manual Installation (From ZIP)
1. Download the latest `qgis_ai_copilot.zip` release from this repository.
2. In QGIS, open **Plugins** -> **Manage and Install Plugins...** -> **Install from ZIP**.
3. Select the `.zip` archive and click **Install Plugin**.

---

## How to Use

1. Click the **AI Copilot** icon on the **Vector** toolbar or open it via **Vector** -> **AI Copilot**.
   The *Data link* section can be folded away, and the border between the chat and the controls can be dragged to make the request box bigger.
2. Select your AI provider (e.g., *Google Gemini*, *OpenAI*, *DeepSeek*).
3. Paste your respective API key into the input field.
4. Type your request in natural language. For example:
   - *"Explain the difference between Clip and Intersection, then create a 250m buffer around selected features."*
   - *"Generate a hexagonal grid (500m cells) over the current map extent and calculate centroids."*
   - *"Find all parcels with an area greater than 5 hectares and color them blue."*
   - *"Download all cafes in the current map extent with QuickOSM and buffer them by 100 m."*
   - *"Which of my plugins can calculate isochrones? Use it for the selected point."*
5. Click **Run with AI**. The AI's explanation and the generated code appear in the chat log.
   With **Auto-run code** off (default) you review the code first and press **Run Code**.

> **Warning:** The plugin executes AI-generated Python code inside QGIS with full access to your files and project. Review code before running it, and keep backups of important data.

---

## Commands available to AI code (29)

| Command | Purpose |
|---|---|
| `add_basemap(name)` | OpenStreetMap, Esri World Imagery / Street Map / Topo, CartoDB, OpenTopoMap (the attribution is added to layouts automatically; Google tiles are not offered because of their terms of use) |
| `zoom_to(target)` | Go to "lat, lon", DMS coordinates, a layer, a bbox or a place name |
| `add_points(points)` | Point layer from coordinates |
| `run_alg(id, params)` | Any Processing algorithm; parameters validated, layers by name, results added to the map |
| `find_alg(text)` | Search algorithms, or print the parameters of an exact algorithm id |
| `get_layer(name)` | Layer by name |
| `new_field(name, kind)` | Attribute field that works on every QGIS version (string, int, double, bool, date, datetime) |
| `style(layer, field, labels=...)` | Graduated / categorized colours and labels |
| `find_action(word)` / `run_action(name)` | Any QGIS or plugin button / menu item |
| `plugin(name)` | List plugins, get a plugin object, enable it or open the plugin manager |
| `download_osm(...)` | OpenStreetMap data: any tags, several tags, all objects, by place name |
| `load(source)` | Local file, file URL or web service (XYZ, WMS, WFS, ArcGIS REST) |
| `page_text()` | The whole text of the attached page, for extracting data written in the text |
| `page_table(n)` | All rows of table n of the attached page, e.g. `layer_from_records(page_table(1), "Population")` |
| `read_page()` | Prints the attached page like a person sees it: text, headings, tables and links |
| `explore_link()` | Lists the numbered data of the attached page: tables, embedded data, linked files and services |
| `load_from_link(pick=[n])` | Loads the chosen numbers (the user confirms a sample); without `pick` the user chooses in a dialog |
| `fetch(url)` / `layer_from_records(rows)` | Raw API response; table → layer |
| `ask` / `choose` / `confirm` | Dialogs: question, choice of data, confirmation with a sample |
| `create_layout(title, layers)` | Print layout: map, title, legend, scale bar, north arrow, sources |
| `export(what, path)` | Layout, layer, map image, project or all layers to any format |
| `field_info(layer, field=None)` | Real values of fields: ranges, distinct values |
| `smart_join(table, layer, ...)` / `join_keys(table, layer)` | Join a table to a map layer with automatic key detection |
| `run_in_background(func, *args)` | Slow pure-Python computation without freezing QGIS |

Uncheck **Use installed plugins** to stop sending the plugin list to the AI provider.

---|---|
| `download_osm("amenity", "cafe")` | Download OSM objects (QuickOSM or Overpass API) and add them as layers |
| `download_osm(tags=["building", "highway"])` | Several OSM tags at once; `download_osm()` = all objects in a small area |
| `download_osm("amenity", "hospital", place="Izmir")` | Download inside a named city/district boundary |
| `run_alg("native:buffer", {"INPUT": "Roads", "DISTANCE": 100})` | Any Processing algorithm, parameters validated, results added to the map |
| `get_layer("Roads")` | Layer by name, with a clear error if missing |
| `list_qgis_actions("table")` / `trigger_qgis_action("mActionOpenTable")` | Any QGIS button or menu item |
| `list_plugins()` | Print installed plugins and whether they are active |
| `find_algorithms("keyword", provider=None)` | Search Processing algorithms, including plugin ones |
| `algorithm_help("provider:alg")` | Print parameters of an algorithm |
| `list_plugin_actions("keyword")` | Print menu items added by plugins |
| `trigger_plugin_action("Plugins > X > Y")` | Open a plugin dialog / press its menu item |
| `get_plugin("package")` | The running plugin object (`qgis.utils.plugins`) |
| `enable_plugin("package")` | Enable an installed plugin (asks the user) |
| `open_plugin_manager()` | Open *Manage and Install Plugins* |

| `search_open_data("Izmir population")` | Search HDX / CKAN open data portals |
| `choose_data(title, items)` / `confirm_data(...)` | Let the user pick sources / confirm a data sample |
| `load_from_url(url)` / `load_file()` | Load web or local files (GeoJSON, CSV, ZIP, GPKG, KML, GeoTIFF…) |
| `wikidata_query(sparql)` / `layer_from_records(rows, name)` | Wikidata statistics → layer |
| `download_boundaries("TUR", "ADM2")` | Administrative boundaries (geoBoundaries) |
| `add_web_layer(url, name)` | XYZ / WMS / WFS / ArcGIS REST services |
| `style_graduated(layer, field)` / `style_categorized` / `add_labels` | Styling |
| `create_layout(title, layers)` | Print layout with map, legend, scale bar, north arrow |
| `export_layout(layout, "map.pdf")` | PDF, PNG, JPG, TIFF, SVG |
| `export_layer(layer, "out.xlsx")` / `export_all_layers()` | Any vector/raster format |
| `export_map_image()` / `export_project()` | Map image / QGIS project |

Uncheck **Use installed plugins** to stop sending the plugin list to the AI provider.

---

## Troubleshooting

| Message | What to do |
|---|---|
| `HTTP 401` / `Invalid API key` | Copy the key again. Make sure it belongs to the selected provider. |
| `HTTP 400` with Gemini | Usually an invalid key or unavailable model. |
| `HTTP 402` / `429` | No balance or quota exceeded on the provider account (a short 429 limit is retried automatically). |
| `HTTP 403` | The key has no access or the service is unavailable in your region. |
| `HTTP 404` | Model not found — press Refresh and choose another model. |
| `SSL certificate check failed` | An antivirus or corporate proxy intercepts HTTPS. Add its certificate in QGIS: *Settings -> Options -> Authentication*. |
| `network error` | Check the internet connection and the proxy in *Settings → Options → Network*. |
| "No downloadable data found on this page. Ask the user for a direct link…" | This message comes from an **old version** (1.0 or older). Uninstall every *AI Copilot* entry in the plugin manager, delete the old plugin folder (e.g. `qgis-ai-copilot-main`), restart QGIS and install the new ZIP. |
| `HTTP 503` / `529` (model busy, "high demand") | The provider is temporarily overloaded. The plugin retries automatically 3 times; if it still fails, wait a minute or choose another model. |
| Answers take long ("model is thinking") | Keep *Fast answers* on and prefer a Flash model; Pro / reasoning models think longer by design. Press *Clear* when you start an unrelated task, so the memory stays short. |
| `Cannot reach Ollama` | Start Ollama (`ollama serve`) and pull a model (`ollama pull qwen2.5-coder`). |

---

## Requirements and restrictions

- QGIS 3.28 or newer (Qt5), or QGIS 4 (Qt6). Windows, Linux and macOS; pure Python, no binaries.
- No additional Python libraries are needed (Shapely is used only if it is already installed).
- An AI provider:
  - a cloud account with an API key: [Google AI Studio](https://aistudio.google.com/) (free tier available),
    [OpenAI Platform](https://platform.openai.com/), [DeepSeek Platform](https://platform.deepseek.com/),
    [Anthropic Console](https://console.anthropic.com/) (paid services), **or**
  - [Ollama](https://ollama.com), installed separately, for local models (no account, no internet).
- Internet access for cloud AI providers, basemaps, OpenStreetMap downloads (Overpass API, Nominatim) and data
  links. All requests use the QGIS network settings (*Settings -> Options -> Network* and *Authentication*).
- Please respect the usage policies of the online services (OpenStreetMap tiles, Overpass API, Nominatim, Esri,
  CARTO): the plugin is meant for interactive use, not for bulk downloads.

---

## Privacy

The following is sent to the AI provider you select: your request, a summary of the project (layer and field
names, CRS, map extent, installed plugin names; with *Layers for AI* you limit which layers are described) and
the printed results of the code that was run. With *Field value hints for the AI* on (default), the value ranges and up to six typical values of the fields of the working layers are sent too; switch it off to send field names only. Other feature data is not sent unless the code prints it. Saved scenarios are kept in `ai_copilot_scenarios.json` in the QGIS profile folder.
API keys are stored in your QGIS user profile. The request archive (`ai_copilot_history.json` in the QGIS profile folder) stays on your computer and never contains API keys; **History > Clear archive** deletes it. With **Ollama** nothing leaves your computer.

---

## Demo test

The `test_data` folder contains a demo test, **"Terrain of Izmir in one minute"**: the AI opens a satellite map,
goes to Kadifekale in Izmir, places random points with heights, builds a TIN, contours and a hillshade and exports
a PDF layout. It includes ready-made requests and a reference script (`check_demo.py`) that runs the same scenario
without the AI and prints OK / FAIL checks. See [test_data/README.md](test_data/README.md).

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPLv3)**. See the [LICENSE](LICENSE) file for details.
