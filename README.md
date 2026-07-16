# SRHD XenoModKit 0.8.4

Headless modding toolkit for **Space Rangers HD: A War Apart** / **Космические рейнджеры HD: Революция**.

Публичная GitHub-версия универсального **SRHD ModKit** для анализа, изменения и безопасной сборки модов. Автор: **[Xenomorphchyma](https://github.com/Xenomorphchyma)**.

> Публичное название — SRHD XenoModKit. Внутренние имена `SRHD ModKit`, `srhd_modkit`, `srhd.py` и `srhd.cmd` сохранены для совместимости.

## Что умеет

- проверять мод целиком и выпускать воспроизводимый ZIP с SHA-256-манифестом;
- читать и изменять BlockPar `DAT` без ручного открытия редактора;
- анализировать, декомпилировать, сравнивать, изменять и собирать `RSON`, `SVR` и `SCR`;
- обнаруживать опасные runtime-шаблоны, связанные с зависанием на «Проходит время»;
- проверять регистрацию скриптов в `Main.dat` и согласованность `CacheData`;
- находить ошибки CP1251, UTF-8 и повреждённый русский текст до запуска игры;
- нативно проверять и преобразовывать `GI ↔ PNG` без RangerTools или Pillow;
- читать, проверять и извлекать `GAI`, `HAI` и `PKG`;
- детерминированно собирать подтверждённые разновидности `GAI` и `PKG`;
- анализировать зависимости и конфликты активного набора модов;
- сохранять неизвестные форматы побайтно и отмечать неполное покрытие.

ModKit не устанавливает моды, не изменяет игру или `ModCFG.txt` и не требует GUI.

### Что изменилось в 0.8.4

- Удалена runtime-зависимость `convert gi-png`/`png-gi` от двух RangerTools EXE.
- Добавлены собственные детерминированные PNG- и GI-кодеки без обязательных пакетов.
- Релизный аудит теперь глубоко проверяет заголовки, слои, смещения и RLE поддерживаемых GI.
- Результат конвертации собирается в staging и читается обратно до публикации.
- Неподдерживаемые старые типы GI не повреждаются и получают явный `unsupported`.

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
| GI ↔ PNG, включая режимы `0_32`, `0_16`, `2` | да | — |
| GAI/HAI/PKG чтение и проверка | да | — |
| GAI/PKG сборка с обратной проверкой | да | — |
| неизвестные форматы и SHA-256-манифест | да | — |
| DAT ↔ TXT и полный DAT-аудит | после setup | BlockParEditor 1.9 |
| RSON/SVR ↔ SCR | после setup | RScript 4.10f |

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
python -B srhd.py script decompile C:\Work\Mod_Name.scr C:\Work\Mod_Name.rson `
  --lang-dat C:\Work\Lang.dat --json
python -B srhd.py script compare-scr C:\Work\Original.scr C:\Work\Patched.scr --json
python -B srhd.py script set-code C:\Work\Script.rson C:\Work\Script.edited.rson `
  --id 17 --field OnActCode --code-file C:\Work\player-buy-handler.txt
python -B srhd.py script build C:\Work\Script.rson --scr C:\Work\Script.scr --lang C:\Work\Lang.txt
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

Для больших проектов лимиты RScript рассчитываются по размеру RSON/SCR и не
имеют верхнего порога. Явный `--timeout 0` у `script build` либо
`--decompile-timeout 0 --roundtrip-timeout 0` у декомпиляции и сравнения
полностью отключает общий дедлайн.

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
- `script validate` до запуска RScript ловит незакрытые строки/комментарии/скобки и случайный русский текст вне строки или комментария — известную причину зависания старого компилятора.
- `script decompile` управляет декомпилятором RScript только на изолированном невидимом desktop, не изменяет исходный SCR, выдаёт поэтапный JSON и публикует RSON лишь после цикла `SCR → RSON → SCR`.
- Машинные отчёты декомпиляции и сравнения имеют схемы `srhd-modkit-decompile-v1` и `srhd-modkit-scr-compare-v1`.
- Непроверенное восстановление удаляется; сохранить его можно только по отдельному явному пути `--keep-unverified`. `--deep-roundtrip` дополнительно проверяет стабильность числа объектов, связей, строк кода и типов после второго восстановления.
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

В версии 0.8.4 проходит 108 тестов; полный набор проверяет нативные PNG/GI,
лексический preflight, адаптивные таймауты, fail-closed декомпиляцию, глубокий
round-trip и сравнение SCR. На локальном корпусе глубоко проверено 3098 из 3131
GI; оставшиеся 33 корректно классифицированы как `unsupported`. Дополнительно
dev-аудит был выполнен на 418 установленных модах без падений валидаторов.

## Авторство

SRHD XenoModKit создан и поддерживается **Xenomorphchyma**. Сторонние кодеки и форматы принадлежат их соответствующим авторам; они перечислены отдельно и не присваиваются проекту.
