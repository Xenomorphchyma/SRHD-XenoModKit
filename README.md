# SRHD XenoModKit 0.8.0

Headless modding toolkit for **Space Rangers HD: A War Apart** / **Космические рейнджеры HD: Революция**.

Публичная GitHub-версия универсального **SRHD ModKit** для анализа, изменения и безопасной сборки модов. Автор: **[Xenomorphchyma](https://github.com/Xenomorphchyma)**.

> Публичное название — SRHD XenoModKit. Внутренние имена `SRHD ModKit`, `srhd_modkit`, `srhd.py` и `srhd.cmd` сохранены для совместимости.

## Что умеет

- проверять мод целиком и выпускать воспроизводимый ZIP с SHA-256-манифестом;
- читать и изменять BlockPar `DAT` без ручного открытия редактора;
- анализировать, изменять и собирать `RSON`, `SVR` и `SCR`;
- обнаруживать опасные runtime-шаблоны, связанные с зависанием на «Проходит время»;
- проверять регистрацию скриптов в `Main.dat` и согласованность `CacheData`;
- находить ошибки CP1251, UTF-8 и повреждённый русский текст до запуска игры;
- читать, проверять и извлекать `GI`, `GAI`, `HAI` и `PKG`;
- детерминированно собирать подтверждённые разновидности `GAI` и `PKG`;
- анализировать зависимости и конфликты активного набора модов;
- сохранять неизвестные форматы побайтно и отмечать неполное покрытие.

ModKit не устанавливает моды, не изменяет игру или `ModCFG.txt` и не требует GUI.

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

Для полной работы с `DAT` и сборки `RSON/SVR → SCR` запустите:

```powershell
.\scripts\setup-tools.ps1
```

Скрипт скачивает BlockParEditor 1.9 и RScript 4.10f из зафиксированных архивов, проверяет SHA-256 и кладёт их рядом с клоном:

```text
Рабочая папка/
├── SRHD-XenoModKit/
├── BlockParEditor/
└── RScript/
```

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
| GAI/HAI/PKG чтение и проверка | да | — |
| GAI/PKG сборка с обратной проверкой | да | — |
| неизвестные форматы и SHA-256-манифест | да | — |
| DAT ↔ TXT и полный DAT-аудит | после setup | BlockParEditor 1.9 |
| RSON/SVR ↔ SCR | после setup | RScript 4.10f |
| GI ↔ PNG | отдельно | проверенные RangerTools-конвертеры |

Старые RangerTools-конвертеры не включены в репозиторий и автоматическую установку: для них не удалось подтвердить авторитетный канал распространения. При их наличии ожидаются файлы `RangerTools/gi-to-png_ranger-tools.exe` и `RangerTools/png-to-gi_ranger-tools.exe`. Остальные функции ModKit от них не зависят.

## Первые команды

Быстрый аудит во время разработки:

```powershell
python -B srhd.py audit C:\Mods\MyMod --profile dev --json
```

Полная проверка и релиз:

```powershell
python -B srhd.py release check C:\Mods\MyMod --json
python -B srhd.py release build C:\Mods\MyMod C:\Releases\MyMod.zip --json
```

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
python -B srhd.py script build C:\Work\Script.rson --scr C:\Work\Script.scr --lang C:\Work\Lang.txt
```

Совместимость активных модов без изменения `ModCFG.txt`:

```powershell
python -B srhd.py compat "C:\Games\Space Rangers HD\Mods\ModCFG.txt" `
  --mods-root "C:\Games\Space Rangers HD\Mods" --json
```

## Безопасность и границы

- `release build` создаёт staging-копию, проверяет архив повторным чтением и сверяет хэши.
- Ошибки блокируют релиз; предупреждения блокируются только с `--warnings-as-errors`.
- `unsupported` означает неполное покрытие, а не повреждение файла.
- GUI заблокирован по умолчанию и не нужен для штатных сценариев.
- HAI поддерживается только для чтения и проверки.
- Статический анализ не заменяет запуск в игре, проверку сохранений и конкретной комбинации модов.

## Документация

- [Подробное руководство на русском](README_RU.md)
- [Архитектура аудита и границы форматов](AUDIT_RU.md)
- [Скриптинг SRHD и runtime-lint](SCRIPTING_GUIDE_RU.md)
- [Внешние инструменты и SHA-256](THIRD_PARTY_TOOLS_RU.md)
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

В версии 0.8.0 проходят 77 тестов. Дополнительно dev-аудит был выполнен на 418 установленных модах без падений валидаторов.

## Авторство

SRHD XenoModKit создан и поддерживается **Xenomorphchyma**. Сторонние кодеки и форматы принадлежат их соответствующим авторам; они перечислены отдельно и не присваиваются проекту.
