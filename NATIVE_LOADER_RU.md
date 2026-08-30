# XenoNativeLoader и SRHD ModKit

Поддерживаемый контракт: **XenoNativeLoader 0.6.5**, C ABI Host API V1.
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

Если RScript обращается к плагину через `ImportedFunction`, native discovery и
PE-экспорт сами по себе недостаточны. В `CFG/Main.dat` нужны узел
`Data/ScriptLibs/<Library>`, `Path`, сигнатура каждой функции и параметр
`<ScriptName>=<Library>`. `script lint-runtime`, `script audit-mod`, project
build и release-аудит проверяют эту цепочку, включая число аргументов и точный
PE export, не загружая DLL.

`srhd compat` использует для native-модов тот же эффективный порядок, что игра
и Loader: стабильную сортировку активных `CurrentMod` по возрастанию `Priority`
и проверку `Dependence`. Для каждой позиции отчёт показывает число найденных
плагинов. Совпадения runtime ID и эксклюзивных capabilities статически не
объявляются: для этого Loader должен безопасно вызвать Query при запуске игры.
