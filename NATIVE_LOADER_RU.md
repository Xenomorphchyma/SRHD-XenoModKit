# XenoNativeLoader и SRHD ModKit

Поддерживаемый контракт: **XenoNativeLoader 0.6.5+**, C ABI Host API V1;
актуальная проверенная версия — **0.8.0**. Loader и готовые реальные примеры
модов публикуются отдельно в
**[XenoMods](https://github.com/Xenomorphchyma/XenoMods)** и не являются
обязательной зависимостью самого ModKit.
ModKit работает с нативной частью конкретного мода и не устанавливает общие
`dsound.dll`, `XenoCore.dll` или `XenoNative.ini` рядом с `Rangers.exe`.

## Быстрый старт

```powershell
python -B srhd.py native init D:\Work\MyNativeMod --id MyNativeRuntime --json
powershell -File D:\Work\MyNativeMod\SOURCE\Native\build.ps1
python -B srhd.py native validate D:\Work\MyNativeMod --json
python -B srhd.py project build D:\Work --json
```

`native init` создаёт `ModuleInfo.txt`, automatic INI, SDK header, минимальный
C++ plugin, `.def`, MSVC x86 build script и `srhd-modkit.toml`. Для владения
генератором галактики используется явный `--capability galaxy-generator`.

## Discovery

Рекомендуемая структура:

```text
MyMod/
  ModuleInfo.txt
  Native/
    MyNativeRuntime.XenoPlugin.dll
    MyNativeRuntime.XenoPlugin.ini
```

Используйте один из двух вариантов регистрации для каждой DLL, не оба сразу:

```text
Вариант automatic (рекомендуется):
  Native/MyRuntime.XenoPlugin.dll
  Native/MyRuntime.XenoPlugin.ini       [Plugin] Enabled=1, Dll=MyRuntime.XenoPlugin.dll

Вариант manifest:
  XenoNativePlugin.ini                  [Plugin] Enabled=1, Dll=Native\MyRuntime.dll
  Native/MyRuntime.dll
```

`XenoNativePlugin.ini` внутри `Native/` с тем же DLL не нужен, если уже есть
automatic INI; дубликаты дают повторное обнаружение или неверный относительный
путь `Native\Native\...`. Команда `native validate` всегда печатает эту схему,
а JSON-отчёт содержит её в поле `layout`.

Поддерживаются корневой `XenoNativePlugin.ini` и несколько
`Native/**/*.XenoManifest.ini`:

```ini
[Plugin]
Enabled=1
Dll=MyMod.Runtime.dll
Config=MyMod.ini
Legacy=0
```

`*.XenoPlugin.ini` зарезервирован для personal config одноимённой automatic
DLL. Пути `Dll` и `Config` не могут выходить за корень мода. INI читаются как
UTF-8, UTF-16LE BOM или CP1251/ANSI; bool принимает `1/0`, `true/false`,
`yes/no`, `on/off`.

## Граница статической проверки

`native inspect/validate` собственной библиотекой читают PE export directory и
проверяют x86 PE32, флаг DLL, `XenoPlugin_Query`, `XenoPlugin_Initialize`,
manifest, config, дубли discovery и пути. DLL при этом не загружается.

Вызов `XenoPlugin_Query` означал бы исполнение произвольного кода мода. Поэтому
ModKit не объявляет доказанными уникальный plugin ID, фактические
`exclusiveCapabilities`, сигнатуры конкретного `Rangers.exe` и успешность
хуков; JSON содержит `runtime_query_executed=false`. Эти свойства проверяет
XenoNativeLoader при старте игры. `DllMain`/Query должны быть без runtime-побочных
эффектов, а Initialize на неподдерживаемом EXE должен вернуть ошибку до частичной
мутации, чтобы Loader мог безопасно выбрать следующий capability-owner/fallback.

Регрессионный тест `test_native_scaffold_build_cache_and_release_with_real_msvc`
при наличии MSVC x86 и BlockPar создаёт новый scaffold, проверяет отказ при
отсутствующей DLL, собирает её и выполняет две проектные сборки с DAT-артефактом.
Он также проверяет cache hit, состав без исходников и одинаковый SHA-256 двух ZIP.
Кэш относится к DAT/SCR; нативная DLL остаётся явно подготовленным prebuilt-входом.
Без MSVC этот интеграционный тест пропускается, а статические PE/manifest-тесты
остаются доступными.

Если RScript обращается к плагину через `ImportedFunction`, native discovery и
PE-экспорт сами по себе недостаточны. В `CFG/Main.dat` нужны узел
`Data/ScriptLibs/<Library>`, `Path`, сигнатура каждой функции и параметр
`<ScriptName>=<Library>`. `script lint-runtime`, `script audit-mod`, project
build и release-аудит проверяют эту цепочку, включая число аргументов и точный
PE export, не загружая DLL.

### Функции, зарегистрированные самим legacy-плагином

Старые плагины могут добавлять функции непосредственно в таблицу RScript через
хуки Loader. Такие имена не обязаны быть PE-экспортами. ModKit поэтому ищет
точное совпадение вызываемого имени в ASCII/UTF-16 таблицах подключённой DLL.
Совпадение снимает ложную ошибку `runtime-unresolved-user-function`, но честно
выдаётся как `runtime-native-loader-function-unverified`: наличие строки ещё не
доказывает, что текущая версия Loader успешно установила хук. Если INI отключает
DLL, вызов остаётся блокирующей ошибкой.

Для современных и проверенных плагинов можно положить рядом с исходным DLL
машиночитаемый sidecar `*.XenoScriptApi.json`:

```json
{
  "schema": "srhd-modkit-native-script-api-v1",
  "dll": "Galaxy.XenoPlugin.dll",
  "functions": [
    {"name": "StarMapGetObjectCluster", "arity": 1}
  ]
}
```

Sidecar не исполняет DLL и не добавляется в игровой архив автоматически, если
лежит в `SOURCE/Native`. Для каждой функции можно указать несколько допустимых
арностей через массив `arity`; отсутствие арности оставляет только проверку
имени. Неверный манифест, отсутствующая DLL или несовпадение числа аргументов
блокируют аудит. Это позволяет описать такие вызовы, как
`StarMapGetObjectCluster(star)`, не маскируя настоящий `Not link var` при
отключённом или не загрузившемся Native Loader.

`srhd compat` использует для native-модов тот же эффективный порядок, что игра
и Loader: стабильную сортировку активных `CurrentMod` по возрастанию `Priority`
и проверку `Dependence`. Для каждой позиции отчёт показывает число найденных
плагинов. Совпадения runtime ID и эксклюзивных capabilities статически не
объявляются: для этого Loader должен безопасно вызвать Query при запуске игры.

## Проверка совместимости 0.8.0

Оба варианта scaffold `native init` (`--capability none` и `galaxy-generator`)
собраны MSVC как настоящие x86 PE32, прошли `native validate` и затем были
загружены настоящими `dsound.dll`/`XenoCore.dll` 0.8.0 в тестовой папке игры:
Loader нашёл их через `ModCFG.txt`, вызвал Query и Initialize, журнал показал
`plugin_load=loaded` и `capabilities=0x1` для генератора галактики. Теми же
статическими проверками без ошибок обработаны опубликованные нативные моды
XenoMods: XenoBigGalaxy, XenoCoalitionSupplyLines, XenoDomRangers и
XenoEquipmentInflation. Установку хуков в самом Rangers.exe проверяет только
запуск игры; найдутся ли сигнатуры плагина в конкретном EXE, заранее
показывает `exe probe --source <исходник>` (см. README_RU.md, раздел «Новый
Rangers.exe»).
