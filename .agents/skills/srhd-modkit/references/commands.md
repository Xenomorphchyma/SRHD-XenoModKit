# Команды SRHD ModKit 0.8.5

Все команды выполнять из `<MODKIT_ROOT>` — корня репозитория с `srhd.py`. Пути с пробелами заключать в кавычки. Добавлять `--json` для машинного разбора: код `0` означает успех, `2` — найденные блокирующие проблемы, `1` — операционную ошибку.

## Аудит и релиз

```powershell
python -B srhd.py audit "<MOD>" --profile dev --json
python -B srhd.py audit "<MOD>" --profile release --json
python -B srhd.py release check "<MOD>" --json
python -B srhd.py release build "<MOD>" "<RELEASES>/MyMod.zip" --json
```

Дополнения: `--warnings-as-errors`, `--allow CODE`, `--allow CODE:GLOB`, `--exclude GLOB`, `--overwrite`. Служебные JSON создаются рядом с ZIP.

## DAT / BlockPar

```powershell
python -B srhd.py dat tree "<WORK>/Main.dat" --json
python -B srhd.py dat get "<WORK>/Main.dat" Data/Script --json
python -B srhd.py dat decode "<WORK>/Main.dat" "<TEMP>/Main.txt"
python -B srhd.py dat encode "<TEMP>/Main.txt" "<OUT>/Main.dat"
python -B srhd.py dat set "<WORK>/Main.dat" "<OUT>/Main.dat" --node Data/SE/Ship --key Cost --value 1500
python -B srhd.py dat patch "<WORK>/Main.dat" "<OUT>/Main.dat" "<WORK>/patch.json"
python -B srhd.py dat validate "<OUT>/Main.dat" --json
```

Для создания отсутствующего параметра добавлять `--create`; для всех одноимённых — `--all`. Путь повторного узла задавать как `Name[2]`.

## Скрипты

```powershell
python -B srhd.py script audit-mod "<MOD>" --json
python -B srhd.py script lint-runtime "<MOD>" --strict --json
python -B srhd.py script info "<WORK>/Script.rson" --json
python -B srhd.py script validate "<WORK>/Script.rson" --json
python -B srhd.py script search "<WORK>/Script.rson" ScriptRun --json
python -B srhd.py script set-code "<WORK>/Script.rson" "<OUT>/Script.rson" --id 42 --field Code --code-file "<WORK>/code.txt"
python -B srhd.py script set-code "<WORK>/Script.rson" "<OUT>/Script.rson" --id 17 --field OnActCode --code-file "<WORK>/player-buy.txt"
python -B srhd.py script set-events "<WORK>/Script.rson" "<OUT>/Script.rson" --id 17 --event t_OnEnteringForm
python -B srhd.py script build "<OUT>/Script.rson" --scr "<OUT>/Script.scr" --lang "<OUT>/Lang.txt"
python -B srhd.py script decompile "<WORK>/Script.scr" "<OUT>/Script.rson" --lang-dat "<WORK>/Lang.dat" --json
python -B srhd.py script decompile "<WORK>/Script.scr" "<OUT>/Script.rson" --deep-roundtrip --json
python -B srhd.py script compare-scr "<WORK>/Original.scr" "<WORK>/Patched.scr" --json
python -B srhd.py script inspect-scr "<OUT>/Script.scr" --json
```

Также доступны `set-field`, `clone-object`, `add-link`, `delete-link`, `delete-object`, `register` и `convert`. Перед точечным изменением смотреть `python -B srhd.py script <command> --help`.

RScript использует адаптивный лимит без верхнего потолка. Ноль у `build --timeout`, `decompile --decompile-timeout`, `decompile --roundtrip-timeout` и одноимённых параметров `compare-scr` отключает общий дедлайн. Непроверенный RSON сохранять только отдельным явным `--keep-unverified`; штатный output остаётся fail-closed.

## Ресурсы

```powershell
python -B srhd.py formats "<MOD>" --json
python -B srhd.py resource info "<WORK>/anim.gai" --json
python -B srhd.py resource verify "<WORK>/image.gi" --json
python -B srhd.py resource list "<WORK>/resources.pkg" --json
python -B srhd.py resource verify "<WORK>/resources.pkg" --json
python -B srhd.py resource extract "<WORK>/resources.pkg" "<TEMP>/unpacked"
python -B srhd.py resource build-gai "<TEMP>/frames" -o "<OUT>/anim.gai" --template "<WORK>/anim.gai"
python -B srhd.py resource build-pkg "<TEMP>/tree" "<OUT>/resources.pkg" --folder Mods --folder Section --folder ModName
python -B srhd.py convert gi-png "<WORK>/Images" -o "<TEMP>/PNG"
python -B srhd.py convert png-gi "<TEMP>/PNG" -o "<OUT>/Images" --mode 0_32
```

GI/PNG преобразуются собственным кодеком ModKit без RangerTools и Pillow.
`0_32` точен по RGBA; `0_16` и `2` используют подтверждённое квантование.
GI типов `1/3/4` и нулевой холст остаются read-only/passthrough с `unsupported`.

HAI поддерживает только `info`, `list` и `verify`. Альтернативный PKG может получить `unsupported`; не преобразовывать его автоматически.

## Совместимость и служебные операции

```powershell
python -B srhd.py compat "<GAME>/Mods/ModCFG.txt" --mods-root "<GAME>/Mods" --json
python -B srhd.py modcfg "<GAME>/Mods/ModCFG.txt" --mods-root "<GAME>/Mods" --json
python -B srhd.py stage "<MODS>/Original" "<WORK>/Copy" --json
python -B srhd.py compare "<MODS>/Old" "<MODS>/New" --json
python -B srhd.py manifest "<MOD>" -o "<OUT>/MyMod.manifest.json"
python -B srhd.py doctor processes --json
python -B srhd.py doctor processes --terminate --json
```

`stage` требует отсутствующую папку назначения и проверяет каждый скопированный файл по SHA-256.

## Python API

Основные экспорты: `audit_mod`, `audit_collection`, `build_release`, `analyze_modset`, `build_gai`, `build_pkg`, `Toolchain`, `load_blockpar`, `RgbaImage`, `inspect_gi`, `read_gi`, `write_gi`, `read_png`, `write_png`, `verify_gi`, `inspect_gai`, `inspect_hai`, `inspect_pkg`, `inspect_hidden_processes`, `terminate_hidden_processes`. JSON-схемы: `srhd-modkit-audit-v1`, `srhd-modkit-release-v1`, `srhd-modkit-modset-v1`, `srhd-modkit-decompile-v1`, `srhd-modkit-scr-compare-v1`, `srhd-modkit-process-audit-v1`, `srhd-modkit-process-cleanup-v1`.
