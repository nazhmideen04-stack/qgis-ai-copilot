# AI Copilot for QGIS

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![QGIS Version](https://img.shields.io/badge/QGIS-4.x-brightgreen.svg)](https://qgis.org)

**AI Copilot** is a universal artificial intelligence assistant plugin for QGIS. It translates natural language instructions into working PyQGIS code and executes spatial workflows directly on your canvas.

---

## Key Features

- **Natural Language Geoprocessing:** Describe what you want to achieve (buffering, layer intersections, filtering, spatial statistics), and the AI writes and executes the code.
- **Project Context Awareness:** Automatically reads project CRS, active layers, attribute schema, and geometry types to avoid coordinate system mismatches.
- **Multi-Provider Support:**
  - Google Gemini (Gemini Flash)
  - OpenAI (GPT-4o)
  - DeepSeek (deepseek-chat)
  - Anthropic Claude (Claude 3.5 Sonnet)
- **Persistent Key Storage:** Securely stores your API tokens locally using `QgsSettings`.
- **PyQt6 & QGIS 4 Ready:** Native support for the modern QGIS 4 environment.

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

1. Click the **AI Copilot** icon on the toolbar or open it via the **Plugins** menu.
2. Select your AI provider (e.g., *Google Gemini*, *OpenAI*).
3. Paste your respective API key into the input field.
4. Type your request in natural language. For example:
   - *"Create a 250m buffer around selected features of the active layer and add it to the project."*
   - *"Find all parcels with an area greater than 5 hectares and color them blue."*
   - *"Generate a temporary point layer with 5 random coordinates inside the current map extent."*
5. Click **Run with AI**. The generated code and execution logs will appear in the output window.

---

## Requirements

- QGIS 4.0 or higher
- Python 3.10+ (bundled with standard QGIS installations)
- An active API key from at least one supported provider:
  - [Google AI Studio](https://aistudio.google.com/)
  - [OpenAI Platform](https://platform.openai.com/)
  - [DeepSeek Platform](https://platform.deepseek.com/)
  - [Anthropic Console](https://console.anthropic.com/)

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPLv3)**. See the [LICENSE](LICENSE) file for details.