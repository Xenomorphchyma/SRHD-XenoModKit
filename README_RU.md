# SRHD ModKit 0.9.8

Публичная GitHub-версия называется **SRHD XenoModKit**. Внутренние имена
каталога, Python-пакета и CLI не переименованы. Автор и сопровождающий:
**[Xenomorphchyma](https://github.com/Xenomorphchyma)**.

Локальная Python-библиотека и командная строка для безопасной работы с модами
Space Rangers HD. Запускается на Python 3.12+ и не требует установки пакетов.

Библиотека не устанавливает моды в игру и не меняет `ModCFG.txt`. Она работает
в указанном пользователем каталоге, сохраняет неизвестные бинарные файлы без изменений и
никогда не объявляет закрытый формат «преобразованным», если штатный инструмент
не подтвердил результат.

Для первого запуска, автоматической установки проверенных DAT/script-кодеков и
описания внешних зависимостей начните с [основного README](README.md) и
[THIRD_PARTY_TOOLS_RU.md](THIRD_PARTY_TOOLS_RU.md).

## Новое в 0.9.8

- GAI теперь понимает штатные пустые кадры `(offset=0, size=0)`, прозрачные вложенные GI `0×0` и сжатые кадры `ZL01(GI)`. Эти варианты подтверждены на установленном корпусе и больше не объявляются повреждением.
- Полностью пустая, но структурно корректная GAI выдаёт неблокирующее `resource-empty-animation-placeholder`: это допустимый способ намеренно скрыть изображение, однако автору полезно подтвердить такое назначение.
- Извлечение GAI распаковывает `ZL01` в обычные GI и сохраняет пустые позиции нулевыми `.gi`; обратная сборка с шаблоном сохраняет порядок кадров и auxiliary-блок.
- `Code.Type=Init` больше не смешивается с pre-`GRun` Global-фазой. Доступ к `Player()` в Init не считается ошибкой запуска, официальный read-only bootstrap в Global не создаёт шум, а другое обращение к миру из Global остаётся предупреждением вместо недоказанного блокера.

## Поддержка форматов

| Формат | Что умеет библиотека | Обработчик |
|---|---|---|
| `.gi` | структура слоёв, глубокая проверка, пакетное `GI → PNG`, режимы `0_32`, `0_16`, `2` | собственный headless-кодек |
| `.png` | CRC, фильтры, палитры, 1–16 бит, Adam7 и `PNG → GI` | собственный headless-кодек |
| `.dat` | DAT↔TXT, дерево, чтение/изменение параметров, JSON-патчи, обратная проверка | собственный BlockPar-слой + BlockParEditor 2.1 CLI |
| `.gai` | raw/ZL01-кадры, пустые позиции, список, извлечение GI, детерминированная сборка с шаблоном | собственный headless-кодек |
| `.hai` | проверка заголовка, размеров и физической разметки всех кадров | собственный read-only-инспектор |
| `.pkg` | рекурсивное дерево, ZL02/raw, распаковка и детерминированная сборка | собственный headless-кодек |
| `.qm`, `.qmm` | чтение QM 2/3/4 и QMM 6/7, граф и формулы, JSON, детерминированная сборка QMM 7 | собственный headless-кодек |
| `.scr` | версия, строки и фрагменты кода, настоящий CLI SCR→RSON и сборка | собственный анализатор + RScript 4.15f |
| `.rson`, `.rsm` | граф/модули, экспорт, сборка и смысловой runtime-lint | Python + RScript 4.15f + rsmc |
| `.svr` | legacy-конвертация RSON↔SVR | совместимый RScript 4.10f из setup |
| `.txt`, `.ini`, `.cfg`, `.json` | обычная текстовая обработка | Python |
| `.wav`, `.dds`, `.webm`, `.psd`, `.jpg`, `.bmp`, `.vdo`, архивы, неизвестные | проверка известных сигнатур и точное копирование | standard/passthrough |

`DAT` теперь полностью управляется из консоли. Собственный парсер видит то же
вложенное дерево, которое в редакторе раскрывается стрелками; понимает блоки `^{`
и `~{`, повторяющиеся узлы и параметры. Шифрование и расшифровка выполняются
официальным кодеком BlockParEditor 2.1, после записи дерево обязательно читается
обратно и сравнивается. Версия 2.1 не требует DLL и запускается напрямую; для
обнаруженной старой 1.9 ModKit сохраняет совместимый локальный legacy-manifest.
Системные настройки не меняются, графическая программа не нужна.

`GAI` и штатный древовидный `PKG` читаются и собираются собственной библиотекой
без ResEditor. GAI поддерживает raw- и `ZL01`-кадры, точные пустые позиции
таблицы и прозрачные GI `0×0`; writer сохраняет подтверждённые поля и вспомогательный блок
шаблона. Writer PKG поддерживает вложенные каталоги, блочные потоки `ZL02` и
несжатые записи. После сборки контейнер полностью читается обратно и сравнивается
с входными файлами. Альтернативная разновидность PKG не считается повреждённой:
она получает статус `unsupported` и сохраняется побайтно. `HAI` остаётся
read-only до точного изучения пиксельных слоёв.

## Текстовые квесты без TGE

ModKit читает старые `QM` версий 2/3/4 и `QMM` версий 6/7 собственной
реализацией на Python. TGE и другие редакторы не запускаются и не требуются.
Модель включает параметры, локации, переходы, условия, изменения, строки и
ссылки на медиа. Формулы разбираются отдельным синтаксическим анализатором с
параметрами `[p1]`, диапазонами, арифметикой и логическими операторами.

```powershell
python -B srhd.py quest info D:\work\Quest.qmm --json
python -B srhd.py quest validate D:\work\Quest.qmm --json
python -B srhd.py quest export-json D:\work\Quest.qmm D:\work\Quest.json
python -B srhd.py quest build D:\work\Quest.json D:\work\Quest.edited.qmm --json
python -B srhd.py quest roundtrip D:\work\Quest.edited.qmm --json
```

`quest build` всегда создаёт QMM 7, перечитывает результат и сравнивает его с
исходной моделью. Существующий файл без `--overwrite` не заменяется. Старый QM
можно экспортировать в JSON и собрать как современный QMM; исходный QM остаётся
неизменным. Аудит обнаруживает отсутствующие ссылки, недостижимые локации,
ошибочные диапазоны, неизвестные параметры формул и потенциальные циклы
автоматических пустых переходов.

Полная схема JSON, ограничения и безопасный процесс редактирования описаны в
[QUESTS_RU.md](QUESTS_RU.md).

## Запуск

```powershell
cd D:\SRHD_Modding\Tools\SRHDModKit
python srhd.py --help
```

Либо запустите `srhd.cmd`.

Проверить все локальные обработчики:

```powershell
python srhd.py tools
```

Посчитать используемые форматы и проверить сигнатуры:

```powershell
python srhd.py formats D:\SRHD_Modding\Projects\ModWorkspaces
python srhd.py formats D:\path\file.dat --hash
```

## Универсальный аудит и релиз

```powershell
python srhd.py audit D:\work\MyMod --profile dev
python srhd.py release check D:\work\MyMod
python srhd.py release build D:\work\MyMod D:\Releases\MyMod.zip
python srhd.py compat "D:\Game\Mods\ModCFG.txt" --mods-root "D:\Game\Mods"
```

`dev` выполняет быстрый цикл разработки, `release` проверяет каждый DAT и каждый
поддерживаемый ресурс. Отчёт перечисляет проверки со статусами `passed`,
`issues`, `skipped`, `unsupported` или `failed`; поэтому неизвестный формат не
выдаётся за успешно разобранный. Ошибки блокируют релиз, предупреждения можно
сделать блокирующими через `--warnings-as-errors`.

Осознанное исключение задаётся как `--allow CODE` либо `--allow CODE:GLOB`.
Проблема не исчезает из JSON, а помечается `suppressed` вместе с правилом.
Безопасный `release build` работает через проверенную staging-копию, повторно
читает ZIP и сверяет путь, размер и SHA-256 каждого файла. Рядом создаются
`*.manifest.json` и `*.audit.json`; внутрь игрового ZIP они не попадают.

`compat` читает ModCFG без изменений, строит зависимости и циклы, различает
идентичные, бинарные, языковые, скриптовые и BlockPar-пересечения. Пока правило
наложения движка не подтверждено, результат намеренно содержит
`resolution: unknown`, а не выдуманного победителя.

## GAI, HAI и PKG без GUI

```powershell
python srhd.py resource info D:\path\animation.gai
python srhd.py resource list D:\path\animation.gai
python srhd.py resource extract D:\path\animation.gai D:\work\frames
python srhd.py resource build-gai D:\work\frames -o D:\work\animation.gai `
  --template D:\path\animation.gai

python srhd.py resource info D:\path\ships.hai
python srhd.py resource list D:\path\ships.hai

python srhd.py resource list D:\path\resources.pkg
python srhd.py resource verify D:\path\resources.pkg
python srhd.py resource extract D:\path\resources.pkg D:\work\unpacked
python srhd.py resource build-pkg D:\work\unpacked D:\work\resources.pkg `
  --folder Mods --folder MySection --folder MyMod
```

`resource extract` никогда не перезаписывает существующие файлы без
`--overwrite`. Перед извлечением PKG полностью проверяется, а имена защищены от
абсолютных путей и `..`. Оба writer детерминированы и выполняют проверку
`encode → decode`; загрузка созданного ресурса самой игрой остаётся отдельной
runtime-проверкой. Writer HAI намеренно отсутствует.

## Преобразование GI и PNG

Кодеки реализованы внутри Python-библиотеки. RangerTools, Pillow и другие
программы для этих команд не требуются.

Один файл:

```powershell
python srhd.py convert gi-png D:\path\image.gi -o D:\path\png
python srhd.py convert png-gi D:\path\image.png -o D:\path\gi --mode 0_32
```

Целая папка с сохранением вложенных путей:

```powershell
python srhd.py convert gi-png D:\path\Data -o D:\path\EditablePNG
```

Режимы создаваемого `GI`:

- `0_32` — RGBA8 в штатной битовой раскладке GI, рекомендуется для рабочего оригинала; проверенный круговой
  проход `PNG → GI → PNG` сохраняет пиксели точно;
- `0_16` — RGB565, меньший размер и потеря точности цвета;
- `2` — три RLE-слоя: RGB565 для непрозрачных и полупрозрачных пикселей плюс
  отдельная 6-битная альфа.

Для результата внутри `DATA\ItemsUseless` рекомендуется `--mode 2`: в
установленном корпусе 310 из 312 предметных GI используют именно type 2 с
тремя слоями, а type 0 с одним слоем не найден. Другой явно выбранный режим не
блокируется; команда и Python API возвращают рекомендацию
`gi-items-useless-mode-2-recommended`.

`resource info --json` и `resource verify --json` для декодируемого GI содержат
`alpha_geometry`: непустые bounds (правая и нижняя границы исключительные),
прозрачные поля, их асимметрию, взвешенный по альфе центр и его смещение от
центра холста. Это объективная диагностика обрезания и полей, но не
семантическая оценка художественного центра.

PNG-декодер принимает штатные цветовые типы PNG, глубину 1–16 бит, палитры,
прозрачность и Adam7, приводя результат к RGBA8. В GI полностью декодируются
типы `0` и `2`. Типы `1`, `3`, `4` и редкие служебные изображения с нулевым
холстом доступны для `resource info` и побайтового сохранения, но получают
`unsupported` при преобразовании.

Конвертация сначала выполняется во временную папку. Результаты проверяются по
сигнатуре и только затем переносятся в назначение. Существующие файлы не
перезаписываются без явного `--overwrite`.

## DAT без GUI

Расшифровать, посмотреть дерево и прочитать параметр:

```powershell
python srhd.py dat decode D:\path\Main.dat D:\work\Main.txt
python srhd.py dat tree D:\path\Main.dat
python srhd.py dat get D:\path\Main.dat Data/SE/Ship --key Cost
```

Изменить один параметр и получить новый, проверенный DAT:

```powershell
python srhd.py dat set D:\path\Main.dat D:\work\Main.dat `
  --node Data/SE/Ship --key Cost --value 1500
```

Для серии правок используется UTF-8 JSON:

```json
{
  "operations": [
    {"op": "set", "node": "Data/SE/Ship", "key": "Cost", "value": "1500"},
    {"op": "set", "node": "Data/SE/Ship", "key": "MyFlag", "value": "1", "create": true},
    {"op": "add-node", "parent": "Data/SE", "name": "MyBlock", "operator": "^"},
    {"op": "delete-parameter", "node": "Data/SE/Ship", "key": "OldFlag"},
    {"op": "delete-node", "node": "Data/SE/Obsolete"}
  ]
}
```

```powershell
python srhd.py dat patch D:\path\Main.dat D:\work\Main.dat D:\work\changes.json
python srhd.py dat validate D:\work\Main.dat
```

Повторяющиеся соседние узлы адресуются как `Ship[2]`. Для `CacheData.dat`
сохраняется исходное имя, поскольку оно влияет на вариант шифрования.
BlockParEditor 2.1 запускается напрямую на служебном рабочем столе; для
обнаруженной 1.9 используется совместимая производная копия. Окна
`Run-time error`, `Runtime error` и `Overflow` возвращаются как обычная ошибка
CLI и не блокируют сеанс. Кириллица поддерживается: проверен полный круговой
проход всех 63 локальных DAT, включая русские `Lang.dat` и `CacheData.dat`.
При сборке BlockPar полезная нагрузка теперь всегда записывается в штатной
Windows-1251. UTF-8 внутри `CFG/Rus/*.dat` мог пройти обратную конвертацию без
потерь, но отображался в игре как `РџС...`; теперь это блокирующая ошибка.
Символы вне CP1251 (например `→`), уже испорченный текст и знак замены `�`
также обнаруживаются до сборки. Для `ModuleInfo.txt` разрешены подтверждённые
штатным корпусом UTF-16LE/BE и CP1251, но не UTF-8 с кириллицей.
Пустой языковой файл RScript `DATA/Script/Lang.dat`, состоящий ровно из
UTF-16LE BOM `FF FE`, распознаётся и валидируется напрямую без BlockParEditor.
Исключение привязано к точному пути и сигнатуре: обычные `CFG/*.dat` продолжают
проходить полную BlockPar-проверку.

## Скрипты без GUI

`RSON` — JSON-проект визуального графа, а не один текстовый файл с кодом. Команды
умеют проверять номера объектов, родителей и связи, искать код в `Code`, `ActCode`
и `LinkCode`, менять выбранный массив и собирать `SCR`:

```powershell
python srhd.py script info D:\work\MyScript.rson
python srhd.py script validate D:\work\MyScript.rson
python srhd.py script search D:\work\MyScript.rson "ScriptRun"
python srhd.py script get D:\work\MyScript.rson 42
python srhd.py script list-links D:\work\MyScript.rson
python srhd.py script set-code D:\work\MyScript.rson D:\work\MyScript.new.rson `
  --id 42 --field Code --code-file D:\work\object42.txt
python srhd.py script set-code D:\work\MyScript.rson D:\work\MyScript.state.rson `
  --id 17 --field OnActCode --code-file D:\work\player-buy-handler.txt
python srhd.py script set-field D:\work\MyScript.rson D:\work\MyScript.named.rson `
  --id 42 --field Name --value '\"Новый объект\"'
python srhd.py script set-events D:\work\MyScript.rson D:\work\MyScript.events.rson `
  --id 17 --event t_OnEnteringForm --event t_OnPlayerBuyEq
python srhd.py script clone-object D:\work\MyScript.rson D:\work\MyScript.clone.rson `
  --id 42 --name "Копия объекта"
python srhd.py script add-link D:\work\MyScript.clone.rson D:\work\MyScript.linked.rson `
  --begin 42 --end 43 --nom 0
python srhd.py script delete-object D:\work\MyScript.linked.rson D:\work\MyScript.deleted.rson `
  --id 43 --detach-references
python srhd.py script build D:\work\MyScript.rson `
  --scr D:\work\MyScript.scr --lang D:\work\MyScript.txt
python srhd.py script export-rsm D:\work\MyScript.rson D:\work\MyScriptRsm --split
python srhd.py script validate-rsm D:\work\MyScriptRsm\main.rsm --json
python srhd.py script build-rsm D:\work\MyScriptRsm\main.rsm `
  --scr D:\work\MyScript.scr --json
python srhd.py script convert D:\work\MyScript.rson D:\work\MyScript.svr # legacy 4.10f
python srhd.py script decompile D:\work\Mod_Name.scr D:\work\Mod_Name.rson `
  --lang-dat D:\work\Lang.dat --json
python srhd.py script compare-scr D:\work\Original.scr D:\work\Patched.scr --json
python srhd.py script register D:\work\CFG\Main.dat D:\out\CFG\Main.dat `
  --name Mod_MyScript --flag 1
python srhd.py script audit-mod D:\path\MyMod
```

`export-rsm` требует RScript 4.15f и создаёт один UTF-8 RSM либо каталог
`main/vars/world/places/states/dialogs/code`. `validate-rsm` не ограничивается
синтаксисом: временно вызывает rsmc и проводит `SCR → RSON → SCR`, runtime-lint
и проверку языка. `build-rsm` делает то же в staging и публикует SCR/Lang только
при `verified: true`. Для `--lang-txt`/`--lang-dat` ModKit заранее создаёт
BlockPar с `Script/<ScriptName>`, потому что сам rsmc умеет только сливать в уже
существующий файл. Импорты RSM и их SHA-256 перечисляются в JSON.

`script decompile` принимает SCR версий 6, 7 и 8. Исходный бинарник только
читается, а вся операция выполняется в маркированной временной транзакции.
JSON перечисляет фазы восстановления, структурной проверки, runtime-lint,
контрольной компиляции и публикации вместе с фактическим временем каждой фазы.
Итог публикуется только после успешного цикла `SCR → RSON → SCR`; поле
`roundtrip.exact_binary_match` отдельно сообщает, совпал ли повторный SCR
побайтно. Непроверенный RSON по штатному пути не появляется. Если он нужен для
ручного разбора, его можно сохранить только отдельным явным параметром
`--keep-unverified D:\work\Script.unverified.rson`.

`--deep-roundtrip` добавляет второй проход `SCR → RSON → SCR → RSON` и требует
стабильности версии, числа объектов, связей, строк кода и набора типов. Поле
`canonical_graph_match` честно показывает более сильное точное совпадение графа,
но оно не обязательно для декомпилированных старых проектов: RScript может
нормализовать внутреннее представление. `script compare-scr` выполняет
проверенное временное восстановление двух бинарников, сравнивает эти метаданные,
блоки кода, persistent-схему, смысловую карту диалогов,
добавленные/устранённые runtime-замечания и `update_issues`, не сохраняя RSON
мода. `runtime-saved-script-cache-update-shadow` появляется только здесь и
только если SHA-256 бинарников различается при одинаковом `ScriptName`.

При внутренней ошибке импорта непустого Lang.dat результат содержит
`lang_import.diagnostic` с доступными сведениями о временном файле. По умолчанию
операция прекращается. Явный `--fallback-without-lang` повторяет восстановление
без языка, выполняет обязательный round-trip и помечает обе стороны сравнения
как `dialogs_imported: false`; это не считается успешным импортом диалогов.

`--lang-dat` необязателен и восстанавливает тексты диалогов из указанного
языкового DAT. В 4.15f используется настоящий CLI без управления формой;
legacy-форма 4.10f работает на отдельном невидимом Windows desktop и не требует
ручных кликов. Транзакции старше суток, оставшиеся
после аварийного завершения ModKit, удаляются только при наличии внутреннего
маркера инструмента; посторонние каталоги с похожим именем не затрагиваются.

По умолчанию небольшой проект получает скользящее 60-секундное окно без
подтверждённого прогресса и не менее 600 секунд общего времени. Для крупных
RSON/SCR оба значения автоматически растут по размеру, числу объектов и строк
кода. Изменение ожидаемого файла, файловый ввод-вывод процесса или переход шага
скрытой автоматизации сдвигают окно; простая загрузка CPU прогрессом не
считается. Положительный `--timeout`, `--decompile-timeout` или
`--roundtrip-timeout` задаёт явный общий предел, а ноль отключает оба
ограничения. Статический preflight всё равно выполняется до старого компилятора.

Перед компиляцией `script build` автоматически запускается смысловой линтер.
Если в моде есть `CacheData`, та же предсборочная проверка сопоставляет локальные
SCR, регистрации `Main` и ссылки `CacheData`. Расхождение исходного
`SOURCE/CFG/CacheData.txt` с игровым `CFG/CacheData.dat`, чужой путь у локального
SCR или отсутствие его ключа останавливают сборку до запуска RScript.
Дополнительные ссылки на SCR зависимостей разрешены.

Отдельно его можно вызвать для всего мода — тогда проверяются одновременно
RSON, собранный `CFG/Main.dat`, исходный `SOURCE/CFG/Main.txt` и `ModuleInfo.txt`:

```powershell
python srhd.py script lint-runtime D:\path\MyMod
python srhd.py script lint-runtime D:\path\MyMod --strict --json
```

Линтер останавливает сборку при следующих доказуемо опасных шаблонах:

- `ScriptRun(ShipStar(Player()), StarPlanets(ShipStar(Player()), 0), ...)`;
- вызов пользовательской функции из другого RSON code object: такой SCR может
  собраться, но игра завершит ход ошибкой `Not link var :ИмяФункции`; исключение
  — функции подтверждённого общего Top с явным `Code.Type=Init`, вызываемые из
  `Code.Type=Turn`;
- чтение из любого runtime code object (`Turn`, `Tif`, `ActCode`, `LinkCode`,
  `OnActCode`) пользовательской переменной, определённой в другом объекте:
  RScript оставляет её несвязанной; объекты `TVar` считаются штатными общими
  переменными проекта;
- пустой `Code=[]`, `ActCode=[]` или `LinkCode=[]` у объекта связанной
  runtime-ветви: даже маленький RSON может навсегда зависнуть в RScript 4.10f;
- достижение `Player`/магазинов/хранилищ/галактики из пошаговой функции до
  раннего `exit`, управляемого событием `t_OnEnteringForm`;
- незаграждённую ветвь визуального Turn-графа: доказательство `CurTurn() > 0`
  переносится downstream только по истинной ветви и только при отсутствии
  альтернативного незащищённого входа;
- работа на первом UI-событии без постановки флага готовности и немедленного
  выхода;
- суммарно вложенные циклы обхода мира в пошаговой цепочке без явного бюджета
  работы на один ход;
- runtime-рекурсия и буквальные `while(1)`/`for(;;)` без выхода;
- обращение к миру из настоящего Global-кода вне узкого read-only bootstrap —
  предупреждение; `Code.Type=Init` выполняется после `GRun` и этим правилом не
  блокируется.

Turn-объекты, достижимые только из `TDialog`, `TDialogMsg`, `TDialogAnswer` или
`DialogBegin`, анализируются как обработчики диалога, а не как периодический
ход игры. Узкий самовызов, который под условием нулевой суммы один раз заменяет
параметры доказанно ненулевыми константами, признаётся ограниченным; остальные
рекурсивные циклы остаются блокирующими.

Структурная проверка RSON теперь включает лексический preflight: строки,
комментарии и пары `()[]{}`. Не-ASCII текст допустим внутри строк и комментариев,
но русская фраза, случайно приклеенная после `;` без `//`, блокируется кодом
`rscript-uncommented-text` до запуска RScript. Это устраняет класс зависаний, при
которых старый компилятор не показывал синтаксическую ошибку.

Неоднозначные, но иногда допустимые конструкции выдаются как предупреждения.
`--strict` делает предупреждения блокирующими. `script audit-mod` включает этот
анализ автоматически, а `script register` не записывает опасный `ScriptRun`.

`set-events` записывает служебную сигнатуру первой строкой `TState.OnActCode`
и сохраняет уже существующий код обработчика. Так подписки `t_On...` создаются,
заменяются и удаляются (`--clear`) полностью без открытия RScript.

Только RScript 4.10f является старой GUI-subsystem программой: после успешной CLI-сборки она
оставляет модальное окно. ModKit запускает её на отдельном невидимом рабочем
столе Windows, ждёт стабильные выходные файлы, проверяет их и завершает процесс.
Поэтому никаких окон и перехвата управления у пользователя нет.

Каждый такой запуск помещается в Windows Job Object с `KILL_ON_JOB_CLOSE`.
Если агент, терминал или сам Python аварийно завершится, Windows уничтожит
редактор и всё созданное им дерево процессов. Запуск сначала приостанавливается
и привязывается к Job, поэтому дочерний процесс не успевает выйти из-под
контроля. Дополнительная очистка выполняется при любом исключении.

Именованный межпроцессный mutex допускает только один внешний SRHD-кодек
одновременно во всей пользовательской сессии. Остальные агенты ждут в
очереди без запуска новых окон; ожидание mutex не расходует адаптивный таймаут
самой компиляции и отдельно отражается в JSON как `*_queue_seconds`. Проверить
или безопасно завершить остатки старых версий можно headless-командами:

```powershell
python -B srhd.py doctor processes
python -B srhd.py doctor processes --terminate
```

`--terminate` действует только на известные `RScript`, `rsmc`, `BlockParEditor` и
`ResEditor`, найденные на служебных desktop с префиксом `SRHDModKit_`.
Невидимый desktop скрывает окна, но не процессы: редакторы остаются обычными
процессами текущего пользователя и видны в Диспетчере задач на вкладке
**Подробности** под исходными именами EXE. Их можно завершить через **Снять
задачу** без повышения прав; ModKit заметит внешний выход и закроет Job и desktop.

Подробности формата и регистрации: [SCRIPTING_GUIDE_RU.md](SCRIPTING_GUIDE_RU.md).

## Ручной GUI (по умолчанию запрещён)

GUI заблокирован двумя независимыми подтверждениями. Даже команда `open` ничего
не запустит, пока человек одновременно не выставит переменную окружения и флаг:

```powershell
$env:SRHD_MODKIT_ALLOW_GUI='1'
python srhd.py open D:\path\animation.gai --allow-gui
```

Автоматические сценарии нейросети не должны выставлять эту переменную. Вернуть
запрет можно командой `Remove-Item Env:SRHD_MODKIT_ALLOW_GUI`.

Точный аудит того, что ещё не автоматизировано: [AUDIT_RU.md](AUDIT_RU.md).

Создать отдельную проверенную рабочую копию всего мода, включая `DAT`, `GI`,
неизвестные форматы и файлы без расширения:

```powershell
python srhd.py stage D:\path\OriginalMod D:\path\WorkingCopy
```

Папка назначения обязана отсутствовать. Каждый скопированный файл проверяется
по размеру и SHA-256, поэтому «тихая» потеря бинарного ресурса исключается.

## Анализ и сборка модов

```powershell
python srhd.py scan D:\SRHD_Modding\Projects\ModWorkspaces
python srhd.py validate D:\SRHD_Modding\Projects\ModWorkspaces
python srhd.py info D:\SRHD_Modding\Projects\ModWorkspaces\XenoBG
python srhd.py compare D:\path\OldMod D:\path\NewMod
python srhd.py collisions D:\SRHD_Modding\Projects\ModWorkspaces --data-only --hash
python srhd.py manifest D:\path\Mod -o D:\path\Mod.manifest.json
python srhd.py pack D:\path\Mod D:\SRHD_Modding\Releases\Mod.zip
python srhd.py release build D:\path\Mod D:\SRHD_Modding\Releases\Mod.zip
```

`pack` сохранён как низкоуровневый совместимый архиватор. Для публикации следует
использовать `release build`, потому что он не создаёт ZIP до успешного аудита.

`ModuleInfo.txt` читается в UTF-16LE, UTF-16BE, UTF-8, CP1251 и CP866, включая
повторяющиеся поля зависимостей и конфликтов.

## Использование как библиотеки

```python
from srhd_modkit import (
    RgbaImage, Toolchain, analyze_modset, audit_mod, build_release,
    build_quest_from_json, discover_mods,
    inspect_file, inspect_gi, inspect_quest, inspect_rscript_lang_fragment,
    load_blockpar, read_gi,
    read_png, stage_tree, verify_gi, verify_quest, write_gi, write_png,
)
from srhd_modkit.resources import build_gai, build_pkg, extract_resource, inspect_gai, inspect_hai, inspect_pkg

mods = discover_mods(r"D:\SRHD_Modding\Projects\ModWorkspaces")
print(inspect_file(r"D:\path\Main.dat", include_hash=True))

tools = Toolchain()
tools.convert([r"D:\path\Images"], r"D:\path\PNG", direction="gi-png")
tools.convert_dat(r"D:\path\Main.dat", r"D:\work\Main.txt")
tools.compile_rson(
    r"D:\work\Script.rson",
    r"D:\work\Script.scr",
    r"D:\work\Script.lang.txt",
    lang_dat_output=r"D:\work\Lang.dat",
    lang_base=r"D:\work\KnownGood\Lang.dat",
)
document = load_blockpar(r"D:\work\Main.txt")
document.find_node("Data/SE/Ship").set_parameter("Cost", "1500")
stage_tree(r"D:\path\OriginalMod", r"D:\path\WorkingCopy")
print(inspect_gai(r"D:\path\anim.gai").summary())
print(inspect_pkg(r"D:\path\resources.pkg").listing())
extract_resource(r"D:\path\resources.pkg", r"D:\work\unpacked")
report = audit_mod(r"D:\work\MyMod", profile="release")
release = build_release(r"D:\work\MyMod", r"D:\Releases\MyMod.zip")
modset = analyze_modset(r"D:\Game\Mods\ModCFG.txt", r"D:\Game\Mods")
quest = inspect_quest(r"D:\work\Quest.qmm")
verified = verify_quest(r"D:\work\Quest.qmm")
```

Сборка RSON поддерживает раздельные тома для `--scr` и `--lang`: публикация
результатов выполняется через временный файл на томе назначения, поэтому
Windows-ошибка `WinError 17` не требует GUI или ручного копирования.

## Скилл для Codex

Репозиторий содержит готовый headless-скилл
`.agents/skills/srhd-modkit`. Codex обнаруживает его при работе из корня этого
репозитория или любого вложенного каталога. Явный вызов: `$srhd-modkit`.

Скилл направляет агента к штатным CLI и Python API, запрещает GUI и изменение
установленной игры, требует честно отмечать `unsupported` и завершать изменения
релизным аудитом. Справочник команд хранится рядом со скиллом и не содержит
локальных абсолютных путей, поэтому каталог можно распространять вместе с
ModKit через GitHub.

Если скилл нужен глобально, скопируйте весь каталог в
`$HOME/.agents/skills/srhd-modkit`. Для работы только с этим репозиторием
установка не требуется.

## Тесты

```powershell
python -B -m unittest discover -s tests -v
```

В наборе из 290 тестов используются нативные PNG/GI/QM/QMM-кодеки, RSM/rsmc и локальные BlockParEditor/RScript:
проверяют пиксель-в-пиксель круговой проход RGBA8, все три режима GI, CRC,
палитры и Adam7, ASCII/Unicode DAT,
события TState в собранном SCR, runtime-блокировки, граф RSON, аудит/релиз,
совместимость, круговые проходы GAI/PKG, лексический preflight, адаптивные
таймауты, fail-closed восстановление, Job Object при аварийной остановке,
межпроцессную очередь, внешнее завершение через системный список процессов и
запрет GUI по умолчанию. Новые тесты покрывают версии QM/QMM, формулы, граф,
детерминированный JSON→QMM, единый аудит и подтверждённые runtime-регрессии.
Дополнительно `dev`-аудит обработал все 425 установленных модов без падений
валидаторов; в корпусе распознано 48 QM/QMM. Глубокая структурная проверка прошла для 3098 из 3131 локального
GI-файла; 31 старый GI типов `1/3/4` и 2 нулевых служебных GI отмечены
`unsupported`. Все 21 распознанные разновидности PKG (1802 файла) полностью
распакованы и проверены; 15 альтернативных контейнеров отмечены `unsupported`.
Все 48 установленных пользовательских QMM и 18 независимых старых QM проходят
чтение и смысловой `QM/QMM → QMM → модель` без расхождений.
