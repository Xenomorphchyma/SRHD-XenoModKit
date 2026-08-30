# Проектная сборка SRHD ModKit

`srhd-modkit.toml` сводит сборку мода к короткому headless-workflow:

```powershell
python -B srhd.py project init "D:/Work/ExistingMod" --json
python -B srhd.py project plan --variant release --json
python -B srhd.py project doctor --json
python -B srhd.py project validate
python -B srhd.py project build --variant release --json
python -B srhd.py project deploy --variant earth-test --target game --dry-run --json
python -B srhd.py project publish --variant release --json
python -B srhd.py project clean --json
```

`project init` один раз создаёт консервативный черновик конфигурации вокруг
существующего мода. `project plan` и `doctor` ничего не компилируют и не
оставляют staging-каталогов. `project build` создаёт точную игровую папку без исходников. `project deploy`
собирает её и безопасно заменяет выбранную цель. `project publish` один раз
собирает проверенный состав, затем из него создаёт ZIP, `*.manifest.json`,
`*.audit.json`, provenance сборки и все настроенные игровые папки. Ни одна
команда не запускает игру и не меняет `ModCFG.txt`.

Неоднозначные TXT/DAT или несколько RSON с общей языковой базой `project init`
оставляет в предупреждениях. Такой черновик загружается ModKit, но спорные
правила сборки следует один раз уточнить вручную в TOML.

`project init` также ищет `.csproj`, `.vcxproj`, `CMakeLists.txt`, `.sln` и
поставляемые модом DLL/EXE. Внешний проект добавляется как неподтверждённый
`[[external_builds]]`: пока автор явно не перечислит runtime-файлы и не выберет
`mode = "prebuilt"`, `plan`, `build`, `deploy` и `publish` блокируются. ModKit
намеренно не запускает найденные C++/C# build-команды — произвольный проект не
является доверенным. Подтверждённые бинарники входят в provenance с SHA-256 и
проходят обычный аудит сигнатур, поэтому формально успешный выпуск без DLL или
launcher невозможен.
Решение `.sln`, которое лишь перечисляет уже найденные `.csproj`/`.vcxproj`,
не дублируется. Независимое решение, ссылка на неучтённый проект или отдельный
runtime-файл с именем решения остаются самостоятельной записью.

Для XenoNativeLoader дополнительно распознаётся `SOURCE/Native/build.ps1`.
Он связывается со всеми готовыми `Native/**/*.XenoPlugin.dll` одной записью
`kind = "xeno-native-plugin"`. Найденный в чужом моде скрипт остаётся
`mode = "unconfigured"` и автоматически не запускается. `native init` создаёт
явный prebuilt-проект: DLL сначала собирается поставленным `build.ps1`, затем
project/audit проверяют её наличие, SHA-256, x86 PE32 и ABI exports.

## Минимальный проект

Публикуемый `srhd-modkit.toml`:

```toml
schema = "srhd-modkit-project-v1"
name = "ExampleMod"
mod_root = "RuntimeMod"
prefix = "OtherMods/ExampleMod"
default_variant = "release"
default_target = "game"
build_root = ".srhd-build"
cache_root = ".srhd-cache"
allow = [] # Осознанные CODE[:GLOB], если они действительно нужны проекту.

[variants.release]
script_name = "Mod_Example"

[variants.earth-test]
inherits = "release"
script_name = "Mod_ExampleEarthTest"
overlays = ["overlays/earth-test"]
include = ["TEST/common/**"]
exclude = ["DATA/release-only/**"]

[[artifacts]]
id = "main-dat"
kind = "dat"
source = "Source/Config/Main.txt"
output = "CFG/Main.dat"

[[artifacts]]
id = "worker"
kind = "rson"
source = "Source/Script/${script_name}.rson"
output = "DATA/Script/${script_name}.scr"
lang_fragment = "SOURCE/Script/${script_name}.lang.txt"
lang_dat = "CFG/Rus/Lang.dat"
lang_base = "Source/Lang/Rus.base.dat"
inputs = ["Source/Script/shared"]

[[external_builds]]
id = "launcher"
kind = "dotnet"
project = "RuntimeMod/Source/Launcher/Launcher.csproj"
mode = "prebuilt"
outputs = ["RuntimeMod/DATA/Launcher.dll", "RuntimeMod/Launcher.exe"]

[targets.game]
prefix = "OtherMods/ExampleMod"

[publish]
output = "Releases/${name}-${variant}.zip"
targets = ["game"]
```

Машинный путь не следует коммитить. Он хранится рядом в
`srhd-modkit.local.toml`:

```toml
tools_root = "D:/SRHD_Modding"

[targets.game]
root = "D:/Steam/steamapps/common/Space Rangers HD A War Apart/Mods"
```

Добавьте в `.gitignore`:

```gitignore
srhd-modkit.local.toml
.srhd-build/
.srhd-cache/
```

Локальный TOML рекурсивно дополняет общий. Конфигурация требует Python 3.12,
поскольку используется стандартный `tomllib`; сторонние Python-пакеты не нужны.
`name` и имена таблиц `[variants.NAME]` являются одним переносимым компонентом
имени: разделители каталогов, `..`, управляющие/запрещённые Windows-символы и
зарезервированные имена устройств отклоняются до создания build/release-файлов.

## Артефакты

Поддерживаются четыре `kind`:

- `dat`: BlockPar TXT → DAT с обратным смысловым чтением;
- `rson`: RSON → SCR, при необходимости с `lang_fragment`, `lang_dat` и `lang_base`;
- `rsm`: модульный RSM → SCR через `rsmc`, с теми же языковыми артефактами;
- `copy`: проверенная SHA-256-копия файла или каталога.

`source` расположен относительно каталога проекта. `output`, `lang_dat`,
`lang_txt` и `lang_fragment` — безопасные пути внутри собираемого мода.
Повторяющиеся выходные пути двух артефактов отклоняются до сборки.

Для дополнительных зависимостей, которые не выводятся из `source`, укажите
`inputs = ["path", "directory"]`. Для RSM автоматически хэшируется весь набор
соседних `.rsm`; языковая база и существующие файлы слияния также считаются
входами.

## Варианты

Вариант может наследовать другой через `inherits`. Применение идёт в таком
порядке: базовое дерево `mod_root`, `overlays`, `include`, `exclude`, затем
сборка артефактов. `overlays` копирует содержимое каталога поверх корня мода;
`include` сохраняет путь относительно корня проекта.

Строковые поля варианта становятся переменными. В примере `${script_name}`
меняет имена RSON, SCR и языкового фрагмента без копирования всего мода.
Дополнительные значения задаются в общей `[variables]` или в
`[variants.NAME.variables]`. Доступны встроенные `${name}` и `${variant}`.

Артефакт можно ограничить вариантами:

```toml
[[artifacts]]
id = "test-panel"
kind = "copy"
source = "TEST/panel"
output = "DATA/TestPanel"
variants = ["earth-test", "diagnostic"]
```

## Безопасный кэш

Ключ артефакта включает:

- SHA-256 и размеры исходника, языка и явно связанных файлов;
- раскрытые параметры артефакта и имя варианта;
- определённые версии и SHA-256 `RScript.exe`, `rsmc.exe` и BlockPar;
- SHA-256 кода ModKit, влияющего на компиляцию и проверку.

Кэш хранится изолированно в `.srhd-cache/artifacts`. Файлы записи проверяются
по размеру и SHA-256 до восстановления. Даже при попадании в кэш SCR снова
проверяется как бинарный контейнер, а весь собранный мод обязательно проходит
обычный release-аудит. Опция `--no-cache` полностью отключает чтение и запись
кэша для конкретного запуска.
Для `copy` каталога запись кэша владеет только действительно скопированными
файлами: соседние базовые файлы `mod_root` не попадают в неё и не могут
восстановиться в устаревшей версии.

Кэш обслуживается автоматически после сборки: текущие ключи не удаляются,
для каждого артефакта и варианта остаются не более трёх последних ревизий,
общий мягкий предел составляет 256 записей и 2 ГиБ. Если одна текущая сборка
сама превышает предел, она сохраняется целиком. Повреждённые записи заменяются,
а оставшиеся после аварийного завершения cache-temp старше суток удаляются.
Счётчики `removed_entries`, `removed_bytes`, `retained_entries` и применённые
лимиты находятся в `cache.maintenance` JSON/provenance.

`.srhd-build/<variant>` также не накапливает копии одной версии: игровая папка
варианта каждый раз заменяется точным проверенным составом, а один
`<name>.build.json` перезаписывается атомарно. Временные project/release/deploy
папки удаляются и после штатного исключения. Версионные ZIP и их sidecar JSON в
`Releases` являются пользовательскими результатами и автоматически не
удаляются.

## План и транзакционное развёртывание

До компиляции можно увидеть ключи кэша и точный будущий состав:

```powershell
python -B srhd.py project plan --variant earth-test --json
python -B srhd.py project doctor --variant earth-test --json
```

`project plan` объясняет cache miss через изменение входов, параметров,
инструментов или движка кэша. `project doctor` дополнительно проверяет пути,
цели, обязательные инструменты, размер кэша и оставшиеся `.srhd-*`.

Перед изменением папки игры:

```powershell
python -B srhd.py project deploy --target game --dry-run --json
```

В JSON перечисляются точная цель, `added`, `changed`, `removed`, `identical`,
`excluded` и полный `audit_report`. Низкоуровневый эквивалент для уже готового
дерева:

`project deploy --dry-run` гарантирует только отсутствие изменений в целевой
папке игры. Чтобы показать точный deploy-состав, команда выполняет реальную
проектную сборку и поэтому может обновить `.srhd-cache` и `.srhd-build`.
Полностью пассивный просмотр до компиляции — `project plan`; поле
`operation_semantics` в JSON явно сообщает эти различия.

```powershell
python -B srhd.py release plan "D:/Work/MyMod" "D:/Game/Mods" --prefix OtherMods/MyMod --json
python -B srhd.py release deploy "D:/Work/MyMod" "D:/Game/Mods" --prefix OtherMods/MyMod --dry-run --json
```

Фактический deploy выполняется под lock, сначала создаёт соседнюю staging-папку,
сверяет SHA-256, отводит прежнюю папку и только затем публикует новую. Это не
слияние: устаревшие файлы удаляются вместе со старым деревом.

Если Windows помешал автоматическому откату, единственная резервная копия
остаётся в транзакции:

```powershell
python -B srhd.py doctor deployments "D:/Game/Mods" --json
python -B srhd.py release rollback "D:/Game/Mods/OtherMods/MyMod" --json
python -B srhd.py release cleanup-transactions "D:/Game/Mods" --json
python -B srhd.py release cleanup-transactions "D:/Game/Mods" --apply --json
```

`doctor` ничего не меняет. `cleanup-transactions` без `--apply` только строит
план и никогда не удаляет данные. Транзакция с доступной резервной копией
сохраняется даже с `--apply`; её можно удалить лишь после rollback либо явно с
`--apply --force`. Транзакция активного PID не удаляется даже с `--force`, а
перед удалением ModKit получает lock той же целевой папки.
Lock, оставшийся после аварийного завершения процесса, также показывается
`doctor`. Он не удаляется обычным `--apply`: после проверки мёртвого PID нужна
явная команда `cleanup-transactions --apply --force`. Ссылка или junction под
видом lock/транзакции никогда не удаляется автоматически.

Служебную очистку проекта сначала просматривают:

```powershell
python -B srhd.py project clean --json
python -B srhd.py project clean --build --cache --json
python -B srhd.py project clean --build --cache --apply --json
```

Без `--apply` ничего не удаляется. По умолчанию в план попадают только
служебные workspace старше суток; build/cache добавляются отдельными флагами.

## Результаты

Основные JSON-схемы:

- `srhd-modkit-project-v1` — конфигурация;
- `srhd-modkit-project-build-v1` — результат сборки;
- `srhd-modkit-project-provenance-v1` — хэши входов, инструменты и provenance;
- `srhd-modkit-project-deploy-v1` — план и результат цели;
- `srhd-modkit-project-publish-v1` — единый выпуск;
- `srhd-modkit-project-init-v1` — найденные связи и предупреждения черновика;
- `srhd-modkit-project-plan-v1` — пересборки, cache hit/miss и файловый состав;
- `srhd-modkit-project-doctor-v1` — инструменты, пути, кэш и workspace;
- `srhd-modkit-project-clean-v1` — dry-run/результат очистки;
- `srhd-modkit-build-cache-v1` — внутренняя проверяемая запись кэша;
- `srhd-modkit-deploy-plan-v1` — точный dry-run;
- `srhd-modkit-deploy-v1` — результат точной замены папки;
- `srhd-modkit-manifest-v1` — файловый SHA-256-манифест;
- `srhd-modkit-deploy-transaction-v1` — журнал восстановления.

Python API предоставляет `initialize_project`, `load_project`, `plan_project`,
`doctor_project`, `clean_project`, `build_project`, `deploy_project`,
`publish_project`, `plan_deploy`, `inspect_deployments`, `rollback_deployment`
и `cleanup_deployments`.
