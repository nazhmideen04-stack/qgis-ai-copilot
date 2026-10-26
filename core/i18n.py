"""
Interface language: English (the source texts) or Russian.

The widgets are built with English texts; translate_tree(widget) replaces them by the chosen language and keeps the
English original, so the language can be switched while the panel is open. Messages built at run time
(f-strings) are translated by tr(), which knows exact texts and PATTERNS with placeholders.
The AI answers are not touched: the AI always answers in the language of the request.
Language "auto" follows the QGIS interface language (Settings → Options → General).
"""
import re

from qgis.core import QgsSettings

LANGUAGES = [("auto", "Auto (QGIS language)"), ("en", "English")]
RUSSIAN_LOCALES = ("ru", "be", "kk", "ky")

_lang = "en"

RU = {
    # ---------------------------------------------------------------- main panel
    "AI Copilot — Request": "AI Copilot — запрос",
    "Settings": "Настройки",
    "Show / hide the settings: provider, model, API key and options": "Показать / скрыть настройки: провайдер, "
                                                                       "модель, ключ API и параметры",
    "AI Provider:": "Провайдер ИИ:",
    "Model:": "Модель:",
    "API Key:": "Ключ API:",
    "Interface language:": "Язык интерфейса:",
    "Auto (QGIS language)": "Авто (язык QGIS)",
    "Pick from the list or type the model name manually": "Выберите из списка или впишите название модели",
    "Refresh": "Обновить",
    "Load models (also checks the API key)": "Загрузить модели (заодно проверяет ключ API)",
    "Show": "Показать",
    "Show / hide key": "Показать / скрыть ключ",
    "Not needed for local AI": "Для локального ИИ не нужен",
    "Paste API key here...": "Вставьте ключ API…",
    "Use installed plugins (send plugin list to the AI)": "Использовать установленные модули (список модулей "
                                                          "отправляется ИИ)",
    "Lets the AI use Processing algorithms, menus and objects of your other plugins":
        "ИИ сможет использовать алгоритмы Processing, меню и объекты других ваших модулей",
    "Chat / Output:": "Чат / результат:",
    "Fast answers (less model thinking)": "Быстрые ответы (модель меньше «думает»)",
    "Agent mode: multi-step tasks and automatic error fixing": "Режим агента: многошаговые задачи и "
                                                               "автоисправление ошибок",
    "Auto-run code": "Автозапуск кода",
    "Execute AI code immediately. Off = review first, then press Run Code":
        "Выполнять код ИИ сразу. Выкл. — сначала посмотреть, потом нажать «Запустить код»",
    "Run Code": "Запустить код",
    "Fix with AI": "Исправить с ИИ",
    "Send the error back to the AI and ask for corrected code": "Отправить ошибку ИИ и попросить исправленный код",
    "Clear": "Очистить",
    "Clear chat and AI memory": "Очистить чат и память ИИ",
    "Undo AI": "Отменить ИИ",
    "Undo the last AI action (code, command, scenario or join)":
        "Отменить последнее действие ИИ (код, команду, сценарий или соединение)",
    "Scenarios…": "Сценарии…",
    "Saved scenarios: save a task with its code and run it again without the AI":
        "Сохранённые сценарии: сохраните задачу вместе с кодом и запускайте её снова без ИИ",
    "History": "История",
    "Archive of your requests: search, view and send again": "Архив запросов: поиск, просмотр и повторная отправка",
    "Field value hints for the AI": "Подсказки значений полей для ИИ",
    "Heavy work in background": "Тяжёлые операции в фоне",
    "Ask before risky code": "Спрашивать перед опасным кодом",
    "Even with Auto-run, asks before code that deletes or moves files, runs system commands, runs hidden code or "
    "writes straight into layer files (SHP, GPKG…) — changes that Undo AI cannot revert.":
        "Даже при автозапуске спрашивает подтверждение, если код удаляет или перемещает файлы, вызывает системные "
        "команды, выполняет скрытый код или пишет прямо в файлы слоёв (SHP, GPKG…) — такое «Отменить ИИ» вернуть "
        "не может.",
    "Layers for AI:": "Слои для ИИ:",
    "All layers": "Все слои",
    "Selected in Layers panel": "Выбранные на панели слоёв",
    "Visible layers": "Видимые слои",
    "Chosen list": "Свой список",
    "Choose…": "Выбрать…",
    "Choose the layers the AI should work with": "Выберите слои, с которыми работает ИИ",
    "Data link": "Ссылка на данные",
    "Show / hide the data link field": "Показать / скрыть поле ссылки",
    "The AI reads this page and uses the internet only with your link:":
        "ИИ читает эту страницу и выходит в интернет только по вашей ссылке:",
    "Paste a link to a website, file or API with the data you need…":
        "Вставьте ссылку на сайт, файл или API с нужными данными…",
    "Open": "Открыть",
    "Open the link in your browser": "Открыть ссылку в браузере",
    "none — click to add a link": "нет — нажмите, чтобы добавить",
    "Run with AI": "Спросить ИИ",
    "Stop": "Стоп",
    "Stopping…": "Останавливаю…",
    "e.g. Create a 250 m buffer around selected features\nType / for quick commands without AI · Ctrl+Enter to send":
        "Например: создай буфер 250 м вокруг выбранных объектов\n/ — быстрые команды без ИИ · Ctrl+Enter — отправить",
    "Unknown command — type /help": "Неизвестная команда — введите /help",
    "Limits the internal 'thinking' of models that support it (Gemini 2.5 / 3, OpenAI o3, o4, gpt-5). Much faster "
    "answers; switch off for very complex tasks.":
        "Ограничивает внутренние «размышления» моделей, которые это умеют (Gemini 2.5 / 3, OpenAI o3, o4, gpt-5). "
        "Ответы гораздо быстрее; для очень сложных задач выключите.",
    "The AI may first inspect algorithms/data, then continue, and automatically asks for a fix when code fails "
    "(max 5 steps). With Auto-run off you still confirm every step with Run Code.":
        "ИИ может сначала изучить алгоритмы или данные, затем продолжить, а при ошибке сам попросить исправление "
        "(не более 5 шагов). Без автозапуска каждый шаг вы подтверждаете кнопкой «Запустить код».",
    "Sends the ranges and typical values of fields of the working layers to the AI, so it uses real values in "
    "filters. Switch off to send only field names.":
        "Отправляет ИИ диапазоны и типичные значения полей рабочих слоёв, чтобы в фильтрах были реальные значения. "
        "Выключите, чтобы отправлялись только названия полей.",
    "Processing algorithms run in a background thread: QGIS does not freeze and Stop reacts at once. Switch off "
    "only if an algorithm misbehaves.":
        "Алгоритмы Processing выполняются в фоновом потоке: QGIS не зависает, «Стоп» срабатывает сразу. Выключайте, "
        "только если какой-то алгоритм работает неправильно.",
    "AI-generated Python code will be executed immediately, without review.\n\nThis code runs inside QGIS with full "
    "access to your project and files and can modify or delete data.\n\nEnable auto-run anyway?":
        "Код Python от ИИ будет выполняться сразу, без проверки.\n\nОн работает внутри QGIS с полным доступом к "
        "проекту и файлам и может изменить или удалить данные.\n\nВсё равно включить автозапуск?",
    # ---------------------------------------------------------------- chat messages
    "Memory cleared.": "Память очищена.",
    "Nothing to undo.": "Отменять нечего.",
    "Nothing had to be changed.": "Ничего менять не пришлось.",
    "The AI returned an empty answer.": "ИИ вернул пустой ответ.",
    "Code executed successfully.": "Код выполнен успешно.",
    "Stopped by user.": "Остановлено пользователем.",
    "Cancelled.": "Отменено.",
    "Choose a model (press Refresh) or type its name.": "Выберите модель (кнопка «Обновить») или впишите название.",
    "The project has no layers yet.": "В проекте пока нет слоёв.",
    "Code is ready — press «Run Code» to execute it (or enable Auto-run to run code automatically).":
        "Код готов — нажмите «Запустить код» (или включите автозапуск).",
    "Agent stopped: this needs your action (permission, a data link or a cancelled dialog). Nothing was retried.":
        "Агент остановлен: нужно ваше действие (разрешение, ссылка на данные или закрытый диалог). "
        "Повторов не было.",
    "Agent stopped after several failed fixes. Check the error or rephrase the task.":
        "Агент остановлен после нескольких неудачных исправлений. Проверьте ошибку или переформулируйте задачу.",
    "Risky code was not run.": "Опасный код не запущен.",
    "Show code": "Показать код",
    "Hide code": "Скрыть код",
    "Show all": "Показать всё",
    "Hide": "Скрыть",
    "Output:": "Вывод:",
    "Execution error:": "Ошибка выполнения:",
    "Created files:": "Созданные файлы:",
    "open folder": "открыть папку",
    "You:": "Вы:",
    "AI:": "ИИ:",
    "Undo:": "Отмена:",
    "Scenario:": "Сценарий:",
    "Command (no AI):": "Команда (без ИИ):",
    "Join:": "Соединение:",
    # ---------------------------------------------------------------- status panel
    "Collecting project context…": "Собираю контекст проекта…",
    "Layers, CRS, map extent": "Слои, СК, охват карты",
    "Layers, CRS, map extent, plugins": "Слои, СК, охват карты, модули",
    "Running code…": "Выполняю код…",
    "QGIS may respond slowly while the code works. Press Stop to cancel.":
        "Пока работает код, QGIS может отвечать медленнее. «Стоп» — отменить.",
    "Writing code…": "Пишу код…",
    "AI is answering…": "ИИ отвечает…",
    "Stopping the running code…": "Останавливаю выполнение кода…",
    "Waiting for the current step to react to the cancel request": "Жду, пока текущий шаг отреагирует на отмену",
    "Waiting for the provider to close the stream": "Жду, пока провайдер закроет поток",
    "Reading the attached page…": "Читаю прикреплённую страницу…",
    "Checking the API key and loading the model list": "Проверяю ключ API и загружаю список моделей",
    "Connected. Waiting for the first words — reasoning models may think for up to a minute before answering.":
        "Подключено. Жду первые слова — «думающие» модели могут размышлять до минуты.",
    "Waiting for the opened window…": "Жду открытое окно…",
    "If a dialog opened, finish or close it to continue": "Если открылся диалог, завершите или закройте его",
    "Waiting for your choice…": "Жду вашего выбора…",
    "Waiting for your confirmation…": "Жду подтверждения…",
    "Waiting for your answer…": "Жду ответа…",
    "Waiting for you to choose where to save…": "Выберите, куда сохранить…",
    "Waiting for you to choose a file…": "Выберите файл…",
    "Waiting for you to choose a folder…": "Выберите папку…",
    "Working in the background…": "Работаю в фоне…",
    # ---------------------------------------------------------------- dialogs
    "AI Copilot — request archive": "AI Copilot — архив запросов",
    "Search in requests and answers…": "Поиск в запросах и ответах…",
    "Use again": "Использовать снова",
    "Delete": "Удалить",
    "Clear archive": "Очистить архив",
    "Close": "Закрыть",
    "Delete the whole request archive?": "Удалить весь архив запросов?",
    "AI Copilot — layers for the AI": "AI Copilot — слои для ИИ",
    "Check the layers the AI should work with. It gets their fields and details; other layers are sent by name only.":
        "Отметьте слои, с которыми работает ИИ. Он получит их поля и сведения, остальные — только по названию.",
    "Filter by name…": "Фильтр по названию…",
    "All": "Все",
    "None": "Ничего",
    "Use selected": "Использовать выбранное",
    "Use this": "Использовать",
    "The AI found these data sources. Select what to use (click an item to see details and the link):":
        "ИИ нашёл эти источники данных. Выберите нужное (щёлкните, чтобы увидеть подробности и ссылку):",
    "Use this data?": "Использовать эти данные?",
    "Enable auto-run?": "Включить автозапуск?",
    "AI Copilot — saved scenarios": "AI Copilot — сохранённые сценарии",
    "Save last task with its code…": "Сохранить последнюю задачу с кодом…",
    "The request and the code that worked: runs again later without the AI":
        "Запрос и сработавший код: потом запускается без ИИ",
    "Save current request…": "Сохранить текущий запрос…",
    "Only the text of the request in the box (it is sent to the AI when run)":
        "Только текст запроса из поля (при запуске отправляется ИИ)",
    "Import…": "Импорт…",
    "Export…": "Экспорт…",
    "Search scenarios…": "Поиск сценариев…",
    "Run saved code (no AI)": "Запустить сохранённый код (без ИИ)",
    "Send to AI": "Отправить ИИ",
    "Put in request box": "Вставить в поле запроса",
    "Rename": "Переименовать",
    "Scenario name": "Название сценария",
    "Name of the scenario:": "Название сценария:",
    "Run scenario?": "Запустить сценарий?",
    "Risky code": "Опасный код",
    "Run anyway": "Всё равно запустить",
    "Don't run": "Не запускать",
    "AI Copilot — join a table to the map": "AI Copilot — соединение таблицы с картой",
    "Choose the table and the map layer. The key fields are found automatically by comparing the values; spelling, "
    "case, accents and number formats (\"01\" = 1) are matched too.":
        "Выберите таблицу и слой карты. Ключевые поля находятся автоматически по значениям; написание, регистр, "
        "диакритика и формат чисел («01» = 1) тоже сопоставляются.",
    "Table with data:": "Таблица с данными:",
    "Add table file…": "Добавить файл таблицы…",
    "Map layer:": "Слой карты:",
    "Key field pairs (best first) — select one:": "Пары ключевых полей (лучшая первой) — выберите:",
    "Table field": "Поле таблицы",
    "Map layer field": "Поле слоя",
    "Keys found": "Найдено ключей",
    "Note": "Примечание",
    "Table fields to add:": "Добавить поля таблицы:",
    "Prefix of new fields:": "Префикс новых полей:",
    "How:": "Как:",
    "Join": "Соединить",
    "exact match": "точное совпадение",
    "matched after normalising": "совпало после нормализации",
    "Automatic (live join if keys match exactly, otherwise a new layer)":
        "Автоматически (живое соединение при точном совпадении, иначе новый слой)",
    "Live QGIS join (keeps the original layer, updates with the table)":
        "Живое соединение QGIS (исходный слой, обновляется вместе с таблицей)",
    "New layer with the table columns (fixes spelling / formats)":
        "Новый слой со столбцами таблицы (исправляет написание / форматы)",
    "AI Copilot — field values": "AI Copilot — значения полей",
    "Real values of your layers. Double-click a field or a value to put it into your request — the AI then uses "
    "exactly these names and values.":
        "Реальные значения ваших слоёв. Двойной щелчок по полю или значению вставит его в запрос — ИИ использует "
        "именно эти названия и значения.",
    "<b>Layers</b>": "<b>Слои</b>",
    "<b>Fields</b>": "<b>Поля</b>",
    "<b>Values</b>": "<b>Значения</b>",
    "Filter values…": "Фильтр значений…",
    "Insert into request": "Вставить в запрос",
    "Add a CSV / Excel / other table file to the project and join it":
        "Добавить в проект файл таблицы (CSV, Excel…) и присоединить его",
    '<b>List of commands</b> — one command per line, run as one action (one Undo):<br><code>/buffer $1 ${2:100}</code><br><code>/style $last area_km2</code><br><code>$1</code> … <code>$9</code> — your arguments, <code>${2:100}</code> — argument 2 or 100, <code>$*</code> — all arguments, <code>$last</code> — the layer the previous line created.<br><b>Python</b> — code like AI code; <code>args</code>, <code>kwargs</code>, <code>arg(1, default)</code> hold the arguments.': '<b>Список команд</b> — по одной команде в строке, выполняются как одно действие (одна отмена):<br><code>/buffer $1 ${2:100}</code><br><code>/style $last area_km2</code><br><code>$1</code> … <code>$9</code> — ваши аргументы, <code>${2:100}</code> — аргумент 2 или 100, <code>$*</code> — все аргументы, <code>$last</code> — слой, созданный предыдущей строкой.<br><b>Python</b> — код как у ИИ; аргументы в <code>args</code>, <code>kwargs</code>, <code>arg(1, по умолчанию)</code>.',
    'AI Copilot — my commands': 'AI Copilot — мои команды',
    'New': 'Новая',
    'Save': 'Сохранить',
    'Ask AI to write one…': 'Попросить ИИ написать…',
    'Describe what the command should do; the AI writes it, you check and save it': 'Опишите, что должна делать команда; ИИ напишет её, вы проверите и сохраните',
    'Name:': 'Название:',
    'Usage:': 'Как вызывать:',
    'Description:': 'Описание:',
    'Type:': 'Тип:',
    'List of commands': 'Список команд',
    'Make a command…': 'Сделать командой…',
    'Turn the saved code into your own /command': 'Превратить сохранённый код в свою /команду',
    'Saved. Type': 'Сохранено. Вводите',
    'Check the command and press Save.': 'Проверьте команду и нажмите «Сохранить».',
    'Write: /newcommand <name> <what it should do>, e.g. /newcommand relief hillshade of a DEM layer, 50 % transparent': 'Напишите: /newcommand <название> <что делать>, например /newcommand relief отмывка рельефа по DEM, прозрачность 50 %',
    'The AI did not write a command. Describe it in other words.': 'ИИ не написал команду. Опишите её другими словами.',
    'Command written. Check it in the window and press Save.': 'Команда написана. Проверьте её в окне и нажмите «Сохранить».',
    'Usage: /newcommand <name> <what it does>': 'Использование: /newcommand <название> <что делает>',
    'Name: 2-31 characters, Latin letters, digits and _, starting with a letter (e.g. relief, buf100).': 'Название: 2–31 символ, латинские буквы, цифры и _, начинается с буквы (например relief, buf100).',
    'The command is empty.': 'Команда пустая.',
    'A command cannot call itself.': 'Команда не может вызывать саму себя.',
    'Commands call each other too deep (loop?).': 'Команды вызывают друг друга слишком глубоко (зацикливание?).',
    '$last works only as a whole argument, e.g. /style $last area.': '$last работает только как отдельный аргумент, например /style $last area.',
    "Choose two different layers.": "Выберите два разных слоя.",
    "No fields with matching values were found. Check that the table and the layer describe the same places.":
        "Полей с совпадающими значениями не найдено. Проверьте, что таблица и слой описывают одни и те же места.",
}

# Messages with values: (English regex, Russian template with \1, \2 …)
PATTERNS = [
    (r"^Enter the API key for (.+?) first\.$", r"Сначала введите ключ API для \1."),
    (r"^Enter the API key for (.+?)\.$", r"Введите ключ API для \1."),
    (r"^(.+?): connected, (\d+) models loaded\.$", r"\1: подключено, загружено моделей: \2."),
    (r"^(.+?): no models found\. Type the model name manually\.$",
     r"\1: модели не найдены. Впишите название модели вручную."),
    (r"^Error: (.*)$", r"Ошибка: \1"),
    (r"^Warning: (.*)$", r"Предупреждение: \1"),
    (r"^\(took ([\d.]+)s\)$", r"(заняло \1 с)"),
    (r"^\(code ran ([\d.]+)s\)$", r"(код выполнялся \1 с)"),
    (r"^Connecting to (.+?)…$", r"Подключаюсь к \1…"),
    (r"^Sending request to (.+?)…$", r"Отправляю запрос в \1…"),
    (r"^Model: (.*)$", r"Модель: \1"),
    (r"^(.+?) is thinking…$", r"\1 думает…"),
    (r"^(\d+) characters · (\d+)/s(.*)$", r"\1 символов · \2/с\3"),
    (r"^Agent step (\d+)/(\d+)$", r"Шаг агента \1/\2"),
    (r"^Step (\d+): continue$", r"Шаг \1: продолжение"),
    (r"^Auto-fix attempt (\d+)/(\d+)…$", r"Автоисправление \1/\2…"),
    (r"^Agent stopped: step limit \((\d+)\) reached\. Write a new message to continue\.$",
     r"Агент остановлен: достигнут лимит шагов (\1). Напишите новое сообщение, чтобы продолжить."),
    (r"^Undo AI can: (.*)\.$", r"«Отменить ИИ» может: \1."),
    (r"^Cannot be undone: (.*)$", r"Нельзя отменить: \1"),
    (r"^Memory: (\d+) message\(s\) of your last conversation restored\. Press Clear to start a new conversation\.$",
     r"Память: восстановлено сообщений прошлого разговора: \1. «Очистить» — начать новый разговор."),
    (r"^AI sees all (\d+) layer\(s\) in detail$", r"ИИ подробно видит все слои (\1)"),
    (r"^AI sees (\d+) of (\d+) layer\(s\) in detail, the rest by name only$",
     r"ИИ подробно видит \1 из \2 слоёв, остальные — только по названию"),
    (r"^nothing selected in the Layers panel — AI sees all (\d+) layer\(s\)$",
     r"на панели слоёв ничего не выбрано — ИИ видит все слои (\1)"),
    (r"^no visible layers — AI sees all (\d+) layer\(s\)$", r"нет видимых слоёв — ИИ видит все слои (\1)"),
    (r"^no layers chosen yet — AI sees all (\d+) layer\(s\)$", r"слои ещё не выбраны — ИИ видит все слои (\1)"),
    (r"^attached: (.*)$", r"прикреплено: \1"),
    (r"^Running: (.*)$", r"Выполняю: \1"),
    (r"^Undo: (.*)\nWill (.*)$", r"Отмена: \1\nБудет: \2"),
    (r"^Removed (\d+) layer\(s\) created by the AI\.$", r"Удалено слоёв, созданных ИИ: \1."),
    (r"^Layer '(.*)' is back\.$", r"Слой «\1» возвращён."),
    (r"^Data of '(.*)' restored\.$", r"Данные «\1» восстановлены."),
    (r"^Edits of '(.*)' reverted\.$", r"Правки «\1» отменены."),
    (r"^Joins of '(.*)' restored\.$", r"Соединения «\1» восстановлены."),
    (r"^Style of '(.*)' restored\.$", r"Стиль «\1» восстановлен."),
    (r"^Removed layout '(.*)'\.$", r"Макет «\1» удалён."),
    (r"^Project CRS restored\.$", r"СК проекта восстановлена."),
    (r"^Could not fully restore '(.*)': (.*)$", r"Не удалось полностью восстановить «\1»: \2"),
    (r"^(.+?) (is busy|limits requests|had a server error) \(HTTP (\d+)\) — retrying in (\d+) s$",
     r"\1 занят (HTTP \3) — повтор через \4 с"),
    (r"^Attempt (\d+)/(\d+)\. Press Stop to cancel\. Busy periods are usually short\.$",
     r"Попытка \1/\2. «Стоп» — отменить. Перегрузка обычно короткая."),
    (r"^(.+?) (is busy|limits requests|had a server error) \(HTTP (\d+)\)\. Retrying automatically in (\d+) s "
     r"\(attempt (\d+)/(\d+)\)…$", r"\1 занят (HTTP \3). Автоповтор через \4 с (попытка \5/\6)…"),
    (r"^Run the saved code of '(.*)' now\?\n\nIt runs inside QGIS like AI code\. You can undo it afterwards with "
     r"«Undo AI»\.$", r"Запустить сохранённый код «\1»?\n\nОн выполняется в QGIS как код ИИ. Потом его можно "
     r"отменить кнопкой «Отменить ИИ»."),
    (r"^No scenario named '(.*)'\. Saved: (.*)\.$", r"Сценария «\1» нет. Сохранены: \2."),
    (r"^saved code, (\d+) step\(s\), no AI$", r"сохранённый код, шагов: \1, без ИИ"),
    (r"^Code — (\d+) line\(s\)$", r"Код — строк: \1"),
    (r"^(\d+) more line\(s\)$", r"ещё строк: \1"),
    (r"^Unknown command /(.*?)\. Did you mean: (.*)\?$", r"Неизвестная команда /\1. Может быть: \2?"),
    (r"^Unknown command /(.*?)\. Type /help for the list\.$", r"Неизвестная команда /\1. Список — /help."),
    (r"^Usage: (.*)$", r"Использование: \1"),
    (r"^Delete /(.*)\?$", r"Удалить /\1?"),
    (r"^(\d+) command\(s\) exported\.$", r"Экспортировано команд: \1."),
    (r"^(\d+) command\(s\) imported\.$", r"Импортировано команд: \1."),
    (r"^/(.*) is a built-in command\. Choose another name\.$", r"/\1 — встроенная команда. Выберите другое название."),
    (r"^A command /(.*) already exists\.$", r"Команда /\1 уже есть."),
    (r"^In a command list every line starts with /: (.*)$", r"В списке команд каждая строка начинается с /: \1"),
    (r"^/(.*) cannot be used inside a command list.*$", r"/\1 нельзя использовать внутри списка команд."),
    (r"^Python syntax error in line (\d+): (.*)$", r"Ошибка синтаксиса Python в строке \1: \2"),
    (r"^Argument (\d+) is missing\.$", r"Не хватает аргумента \1."),
]
_COMPILED = [(re.compile(p, re.DOTALL), t) for p, t in PATTERNS]


def resolve(setting):
    """Language code ("en" / "ru") for the setting value auto / en. Auto follows the QGIS interface language."""
    if setting == "en":
        return "en"
    settings = QgsSettings()
    locale = ""
    if str(settings.value("locale/overrideFlag", False)).lower() in ("true", "1"):
        locale = str(settings.value("locale/userLocale", "") or "")
    if not locale:
        try:
            from qgis.core import QgsApplication
            locale = QgsApplication.locale()
        except (ImportError, AttributeError):
            from qgis.PyQt.QtCore import QLocale
            locale = QLocale.system().name()
    return "ru" if str(locale).lower()[:2] in RUSSIAN_LOCALES else "en"


def set_language(setting):
    global _lang
    _lang = resolve(setting)
    return _lang


def language():
    return _lang


def tr(text):
    """Text in the interface language. Unknown texts stay in English."""
    if _lang != "ru" or not text:
        return text
    if text in RU:
        return RU[text]
    for pattern, template in _COMPILED:
        if pattern.match(text):
            return pattern.sub(template, text)
    return text


def tr_lines(text):
    """Translates every line separately (lists of messages joined by newlines)."""
    return "\n".join(tr(line) for line in str(text).split("\n"))


# --------------------------------------------------------------------------- widgets

def dynamic(widget, *keys):
    """Marks texts that the code sets at run time (with tr()), so translate_tree leaves them alone.
    keys: text, tip, placeholder, title (default: all)."""
    widget.setProperty("ai_skip", ",".join(keys or ("text", "tip", "placeholder", "title")))
    return widget


def _swap(widget, key, getter, setter):
    if key in str(widget.property("ai_skip") or "").split(","):
        return
    original = widget.property(f"ai_en_{key}")
    if original is None:
        original = getter()
        if not original:
            return
        widget.setProperty(f"ai_en_{key}", original)
    setter(tr(str(original)))


def translate_tree(root):
    """Translates the texts, tool tips, placeholders and list items of a widget and all its children."""
    from qgis.PyQt.QtWidgets import (QAbstractButton, QComboBox, QDockWidget, QGroupBox, QLabel, QLineEdit,
                                     QTableWidget, QTextEdit, QWidget)
    widgets = [root] + root.findChildren(QWidget)
    for w in widgets:
        if w.isWindow() or isinstance(w, QDockWidget):
            _swap(w, "title", w.windowTitle, w.setWindowTitle)
        _swap(w, "tip", w.toolTip, w.setToolTip)
        if isinstance(w, (QLabel, QAbstractButton)):
            _swap(w, "text", w.text, w.setText)
        if isinstance(w, QGroupBox):
            _swap(w, "text", w.title, w.setTitle)
        if isinstance(w, (QLineEdit, QTextEdit)):
            _swap(w, "placeholder", w.placeholderText, w.setPlaceholderText)
        if isinstance(w, QComboBox) and w.property("ai_translate_items"):
            for i in range(w.count()):
                key = f"ai_en_item{i}"
                original = w.property(key)
                if original is None:
                    original = w.itemText(i)
                    w.setProperty(key, original)
                w.setItemText(i, tr(original))
        if isinstance(w, QTableWidget):
            for col in range(w.columnCount()):
                item = w.horizontalHeaderItem(col)
                if item is not None:
                    original = item.data(256 + 77) or item.text()  # Qt.UserRole + 77 keeps the English text
                    item.setData(256 + 77, original)
                    item.setText(tr(original))


_UNDO_WORDS = {"style": "стиль", "filter": "фильтр", "joins": "соединения", "name": "название",
               "visibility": "видимость", "data": "данные", "unsaved edits": "несохранённые правки"}
_UNDO_PARTS = [
    (r"^remove (\d+) new layer\(s\): (.*)$", r"удалить новые слои (\1): \2"),
    (r"^bring back (\d+) removed layer\(s\): (.*)$", r"вернуть удалённые слои (\1): \2"),
    (r"^remove layout\(s\): (.*)$", r"удалить макеты: \1"),
    (r"^restore the project CRS$", r"восстановить СК проекта"),
    (r"^restore the map view$", r"вернуть вид карты"),
    (r"^data of '(.*)' changed directly in the file$", r"данные «\1» изменены прямо в файле"),
    (r"^removed layer '(.*)' \((.*)\)$", r"удалённый слой «\1» (\2)"),
    (r"^edits of '(.*)' \((.*)\)$", r"правки «\1» (\2)"),
]
_UNDO_COMPILED = [(re.compile(p), t) for p, t in _UNDO_PARTS]


def tr_undo(summary, separator="; "):
    """The «what will be undone» summary of core/undo.py in the interface language."""
    if _lang != "ru" or not summary:
        return summary
    out = []
    for part in summary.split(separator):
        match = re.match(r"^restore (.*) of '(.*)'$", part)
        if match:
            words = ", ".join(_UNDO_WORDS.get(w.strip(), w.strip()) for w in match.group(1).split(","))
            out.append(f"восстановить {words} у «{match.group(2)}»")
            continue
        for pattern, template in _UNDO_COMPILED:
            if pattern.match(part):
                part = pattern.sub(template, part)
                break
        out.append(part)
    return separator.join(out)
