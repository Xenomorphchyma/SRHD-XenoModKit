# SRHD XenoModKit 0.9.9

Headless modding toolkit for **Space Rangers HD: A War Apart** / **Космические рейнджеры HD: Революция**.

Публичная GitHub-версия универсального **SRHD ModKit** для анализа, изменения и безопасной сборки модов. Автор: **[Xenomorphchyma](https://github.com/Xenomorphchyma)**.

> Публичное название — SRHD XenoModKit. Внутренние имена `SRHD ModKit`, `srhd_modkit`, `srhd.py` и `srhd.cmd` сохранены для совместимости.

## Что умеет

- проверять мод целиком и выпускать воспроизводимый ZIP с SHA-256-манифестом;
- читать и изменять BlockPar `DAT` без ручного открытия редактора;
- анализировать, декомпилировать, сравнивать, изменять и собирать `RSON`, модульные `RSM`, legacy-`SVR` и `SCR`;
- обнаруживать опасные runtime-шаблоны, связанные с зависанием на «Проходит время»;
- проверять регистрацию скриптов в `Main.dat` и согласованность `CacheData`;
- находить ошибки CP1251, UTF-8 и повреждённый русский текст до запуска игры;
- нативно проверять и преобразовывать `GI ↔ PNG` без RangerTools или Pillow;
- читать, проверять и извлекать `GAI`, `HAI` и `PKG`;
- детерминированно собирать подтверждённые разновидности `GAI` и `PKG`;
- нативно читать, проверять, редактировать через JSON и собирать текстовые квесты `QM/QMM` без TGE;
- анализировать зависимости и конфликты активного набора модов;
- сохранять неизвестные форматы побайтно и отмечать неполное покрытие.

ModKit разворачивает мод в игру только по явной команде `release deploy`, не
изменяет `ModCFG.txt` и не требует GUI.

### Что изменилось в 0.9.9

- Добавлена `release deploy` для проверенного headless-развёртывания готового мода непосредственно в корень `Mods` игры или в `Builds`.
- Deploy по умолчанию исключает `SOURCE`/`SOURCES`, RSON/RSM/SVR и известные служебные текстовые исходники; `--include-sources` оставляет их для отладочной копии.
- `--overwrite` выполняет полную транзакционную замену дерева вместо копирования поверх старой папки. Новый состав предварительно проверяется по SHA-256, устаревшие файлы исчезают, а при сбое прежняя папка восстанавливается.
- Для ZIP добавлен симметричный режим `release build --strip-sources`; без флага сохранено совместимое поведение открытого релиза с исходниками.
- Deploy не запускает игру, не включает мод и не меняет `ModCFG.txt`.

## Быстрый старт

Требуется Windows 10/11 x64 и [Python 3.12 или новее](https://www.python.org/downloads/windows/). Для проверки поведения мода в игре нужна установленная Space Rangers HD, но путь к игре не требуется для запуска самой библиотеки.

```powershell
git clone https://github.com/Xenomorphchyma/SRHD-XenoModKit.git
Set-Location SRHD-XenoModKit
python -B srhd.py --version
python -B srhd.py --help
```

Обязательных Python-пакетов нет. Установка через `pip` для запуска `srhd.py` не нужна.

Проверить доступность дополнительных кодеков:

```powershell
python -B srhd.py tools
```

### Установить DAT- и script-кодеки

Для полной работы с `DAT`, `RSON/RSM/SCR` и legacy-`SVR` запустите:

```powershell
.\scripts\setup-tools.ps1
```

Скрипт скачивает BlockParEditor 2.1, RScript 4.15f и rsmc из официальных зафиксированных релизов, проверяет SHA-256 архивов и EXE и кладёт их рядом с клоном:

```text
Рабочая папка/
├── SRHD-XenoModKit/
├── BlockParEditor/
├── RScript/
├── RScript410/
└── RSMCompiler/
```

При обновлении уже проверенные 1.9/4.10f не удаляются: установщик переносит их
в `BlockParEditor19/` и `RScript410/`. На чистой установке 4.10f также
добавляется в `RScript410/`, потому что она нужна для legacy `RSON ↔ SVR`,
которой больше нет в CLI 4.15f.

Другой каталог можно задать явно:

```powershell
.\scripts\setup-tools.ps1 -ToolsRoot C:\SRHD-Tools
python -B srhd.py tools --tools-root C:\SRHD-Tools
```

Подробные источники, контрольные суммы и ручная установка описаны в [THIRD_PARTY_TOOLS_RU.md](THIRD_PARTY_TOOLS_RU.md).

## Что работает без дополнительных загрузок

| Возможность | После клонирования | Дополнительный инструмент |
|---|---:|---|
| структура мода, ModuleInfo, пути, мусорные файлы | да | — |
| кодировки и русский игровой текст | да | — |
| SCR binary-аудит и runtime-lint RSON | да | — |
| GI ↔ PNG, включая режимы `0_32`, `0_16`, `2` | да | — |
| GAI/HAI/PKG чтение и проверка | да | — |
| GAI/PKG сборка с обратной проверкой | да | — |
| QM/QMM чтение, JSON-редактирование, сборка и аудит | да | — |
| неизвестные форматы и SHA-256-манифест | да | — |
| DAT ↔ TXT и полный DAT-аудит | после setup | BlockParEditor 2.1 |
| RSON ↔ SCR, настоящий CLI SCR → RSON | после setup | RScript 4.15f |
| RSM export/build/validate | после setup | RScript 4.15f + rsmc |
| RSON ↔ SVR (legacy) | после setup | совместимый RScript 4.10f |

## Первые команды

Быстрый аудит во время разработки:

```powershell
python -B srhd.py audit C:\Mods\MyMod --profile dev --json
```

Полная проверка и релиз:

```powershell
python -B srhd.py release check C:\Mods\MyMod --json
python -B srhd.py release build C:\Mods\MyMod C:\Releases\MyMod.zip --json
python -B srhd.py release deploy C:\Work\MyMod "C:\Games\Space Rangers HD\Mods" --prefix OtherMods/MyMod --overwrite --json
```

`release deploy` принимает корень `Mods`/`Builds`, а `--prefix` — путь мода
внутри него. Существующая целевая папка заменяется только при явном
`--overwrite`; это точная замена, поэтому файлы, удалённые из проекта, не
сохраняются от прежней сборки. Исходники исключены по умолчанию.

Безопасная рабочая копия:

```powershell
python -B srhd.py stage C:\Mods\Original C:\Work\MyMod
```

DAT / BlockPar:

```powershell
python -B srhd.py dat tree C:\Work\MyMod\CFG\Main.dat --json
python -B srhd.py dat decode C:\Work\MyMod\CFG\Main.dat C:\Work\Main.txt
python -B srhd.py dat validate C:\Work\MyMod\CFG\Main.dat --json
```

Скрипты:

```powershell
python -B srhd.py script audit-mod C:\Work\MyMod --json
python -B srhd.py script lint-runtime C:\Work\MyMod --strict --json
python -B srhd.py script decompile C:\Work\Mod_Name.scr C:\Work\Mod_Name.rson `
  --lang-dat C:\Work\Lang.dat --json
python -B srhd.py script compare-scr C:\Work\Original.scr C:\Work\Patched.scr --json
python -B srhd.py script compare-storage C:\Work\Old.rson C:\Work\New.rson --json
python -B srhd.py script set-code C:\Work\Script.rson C:\Work\Script.edited.rson `
  --id 17 --field OnActCode --code-file C:\Work\player-buy-handler.txt
python -B srhd.py script build C:\Work\Script.rson --scr C:\Work\Script.scr --lang C:\Work\Lang.txt
python -B srhd.py script export-rsm C:\Work\Script.rson C:\Work\ScriptRsm --split
python -B srhd.py script validate-rsm C:\Work\ScriptRsm\main.rsm --lang-base C:\Work\Lang.dat --json
python -B srhd.py script build-rsm C:\Work\ScriptRsm\main.rsm --scr C:\Work\Script.scr `
  --lang-dat C:\Work\Lang.dat --lang-base C:\Work\Lang.base.dat --json
```

Текстовые квесты без TGE:

```powershell
python -B srhd.py quest info C:\Work\Quest.qmm --json
python -B srhd.py quest validate C:\Work\Quest.qmm --json
python -B srhd.py quest export-json C:\Work\Quest.qmm C:\Work\Quest.json
python -B srhd.py quest build C:\Work\Quest.json C:\Work\Quest.edited.qmm --json
python -B srhd.py quest roundtrip C:\Work\Quest.edited.qmm --json
```

GI/PNG без дополнительных программ:

```powershell
python -B srhd.py convert gi-png C:\Work\Images -o C:\Work\PNG
python -B srhd.py convert png-gi C:\Work\PNG -o C:\Work\Images --mode 0_32
```

`0_32` сохраняет RGBA пиксель-в-пиксель; `0_16` использует RGB565, а режим
`2` — три RLE-слоя с RGB565 и отдельной прозрачностью. Старые GI типов `1`,
`3`, `4` и служебные GI с нулевым холстом сохраняются без изменений и честно
получают `unsupported`, поскольку их нельзя безопасно представить как PNG.
При выводе в `DATA\ItemsUseless` режим `2` рекомендуется для совместимости
крупного слота и уменьшенных карточек предмета. Если явно выбран другой режим,
конвертация всё равно завершается, а CLI/Python API возвращает рекомендацию
`gi-items-useless-mode-2-recommended`.

Для поддерживаемого GI команды `resource info --json` и
`resource verify --json` включают `alpha_geometry`: непустые границы с
исключительным `finish_x/finish_y`, прозрачные поля, их асимметрию и
взвешенный по альфе центр. Это диагностические числа, а не автоматическая
оценка художественной композиции.

По умолчанию небольшой проект RScript получает 60 секунд без подтверждённого
прогресса и не менее 600 секунд общего времени. Для крупных проектов оба окна
автоматически растут по размеру RSON/SCR, числу объектов и строк кода. Изменение
ожидаемого файла, файловый ввод-вывод процесса или переход шага скрытой
автоматизации сдвигают окно; простая загрузка CPU прогрессом не считается.
Положительный `--timeout` задаёт явный общий предел, а `0` у `script build` или
обоих таймаутов декомпиляции и сравнения отключает оба ограничения.

Совместимость активных модов без изменения `ModCFG.txt`:

```powershell
python -B srhd.py compat "C:\Games\Space Rangers HD\Mods\ModCFG.txt" `
  --mods-root "C:\Games\Space Rangers HD\Mods" --json
```

`load_order` и владельцы пересекающихся путей выводятся в эффективном порядке
движка: стабильная сортировка по возрастанию `Priority`, где отсутствующее поле
равно нулю, а `CurrentMod` разрешает равенство. Исходная позиция остаётся в
`configured_order`; `compat` никогда не переписывает конфигурацию. Даже при
известном порядке сложное наложение сохраняет `resolution: unknown`, пока
семантика конкретного BlockPar/SCR/CacheData-пересечения не доказана.

## Безопасность и границы

- `release build` создаёт staging-копию, проверяет архив повторным чтением и сверяет хэши.
- Ошибки блокируют релиз; предупреждения блокируются только с `--warnings-as-errors`.
- `unsupported` означает неполное покрытие, а не повреждение файла.
- GUI заблокирован по умолчанию и не нужен для штатных сценариев.
- `script validate` до запуска RScript ловит незакрытые строки/комментарии/скобки и случайный русский текст вне строки или комментария — известную причину зависания старого компилятора.
- `script decompile` в RScript 4.15f использует настоящий CLI; 4.10f запускается только как legacy-бэкенд на изолированном невидимом desktop. Исходный SCR не изменяется, а RSON публикуется лишь после цикла `SCR → RSON → SCR`.
- Сбой импорта непустого Lang.dat не скрывается: JSON содержит структурированную диагностику, а восстановление без диалогов выполняется только по явному `--fallback-without-lang`.
- Машинные отчёты декомпиляции, сравнения и совместимости persistent-хранилища имеют схемы `srhd-modkit-decompile-v1`, `srhd-modkit-scr-compare-v1` и `srhd-modkit-storage-compat-v1`.
- Непроверенное восстановление удаляется; сохранить его можно только по отдельному явному пути `--keep-unverified`. `--deep-roundtrip` дополнительно проверяет стабильность числа объектов, связей, строк кода и типов после второго восстановления.
- QM/QMM writer не меняет исходник: JSON собирается в новый QMM, затем файл перечитывается и сравнивается с моделью. Проверка формата не заменяет прохождение квеста в игре.
- HAI поддерживается только для чтения и проверки.
- Статический анализ не заменяет запуск в игре, проверку сохранений и конкретной комбинации модов.

## Документация

- [Подробное руководство на русском](README_RU.md)
- [Архитектура аудита и границы форматов](AUDIT_RU.md)
- [Скриптинг SRHD и runtime-lint](SCRIPTING_GUIDE_RU.md)
- [Текстовые квесты QM/QMM](QUESTS_RU.md)
- [Внешние инструменты и SHA-256](THIRD_PARTY_TOOLS_RU.md)
- [Уведомления о сторонних исследованиях форматов](THIRD_PARTY_NOTICES.md)
- [Авторство](AUTHORS.md)

## Codex skill

В репозитории находится headless-скилл `.agents/skills/srhd-modkit`. При работе Codex внутри клона он обнаруживается автоматически; явный вызов:

```text
$srhd-modkit
```

Скилл требует использовать CLI/Python API, не запускать GUI, не изменять установленную игру и честно сообщать о неполном покрытии.

## Тесты

```powershell
python -B -m unittest discover -s tests -v
```

Текущий набор из 293 тестов проверяет нативные PNG/GI, лексический preflight,
прогресс-зависимые таймауты, fail-closed декомпиляцию, глубокий round-trip,
RScript 4.10f/4.15f, RSM/rsmc, BlockPar 2.1 и завершение скрытого дерева
процессов при обрыве агента, а также
QM/QMM reader/writer, формулы квестов и JSON-цикл. На локальном корпусе глубоко проверено 3098 из 3131
GI; оставшиеся 33 корректно классифицированы как `unsupported`. Дополнительно
dev-аудит был выполнен на 425 установленных модах без падений валидаторов.
В корпусе распознано 48 QM/QMM.

## Авторство

SRHD XenoModKit создан и поддерживается **Xenomorphchyma**. Сторонние кодеки и форматы принадлежат их соответствующим авторам; они перечислены отдельно и не присваиваются проекту.
