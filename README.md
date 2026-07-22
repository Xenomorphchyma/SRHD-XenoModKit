# SRHD XenoModKit 0.9.4

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
- нативно читать, проверять, редактировать через JSON и собирать текстовые квесты `QM/QMM` без TGE;
- анализировать зависимости и конфликты активного набора модов;
- сохранять неизвестные форматы побайтно и отмечать неполное покрытие.

ModKit не устанавливает моды, не изменяет игру или `ModCFG.txt` и не требует GUI.

### Что изменилось в 0.9.4

- Новый межстрочный data-flow отслеживает nullable/raw handles из `GalaxyStar` и `StarRuins`. Перед передачей результата в `Star*`, `Ship*`, `Id` или `RelationToRanger` требуется отдельный доминирующий null-guard; проверка внутри того же `&&`/`||` доказательством не считается.
- `runtime-redundant-star-ruins-type-dereference` предупреждает о лишнем `ShipTypeN` после типизированного `StarRuins(star, 'TYPE')`: достаточно отдельно проверить найденный объект на ноль.
- Расширенное правило `runtime-object-api-behind-boolean-guard` теперь охватывает `ShipTypeN`, `RelationToRanger`, `StarRuins`, `ShipOwner` и другие объектные вызовы, но корректно различает условие и безопасное тело `if(object) Api(object);`.
- `runtime-dialog-inject-delayed-persistent-gate` информационно отмечает `AddDialogInject`, доступность которого зависит только от persistent-флага, устанавливаемого позднее в `Turn`. Намеренные сюжетные задержки не блокируются.
- Runtime-замечания, полученные после `SCR → RSON`, теперь имеют `analysis_origin: decompiled-rson`; чувствительные к канонизации предупреждения дополнительно получают `canonicalization_sensitive: true`. Это не подавляет ошибку, а отделяет восстановленное доказательство от авторского SOURCE RSON.
- Полный набор 0.9.4 состоит из 177 тестов, включая воспроизведение старого аварийного XenoDom и безопасные формы отдельных guard.

### Что изменилось в 0.9.3

- `runtime-shipstar-on-docked-ship` блокирует `ShipStar` для посаженного или ещё взлетающего корабля, если до вызова не доказаны `ShipInNormalSpace` и завершённый `OrderTakeOff`. Проверка работает и внутри функций, и в прямом коде обработчиков.
- Результат `ShipGetBad` считается непрозрачным raw handle: пространственные, `Ship*` и `Order*`-вызовы запрещены, пока объект не сопоставлен с живым кораблём из текущей системы. Поскольку `StarShips` включает станции, перед `ShipIsTakeoff` дополнительно требуется доказательство `ShipTypeN < t_RC`.
- Persistent `TVar`, переданный в `Array*` без инициализации `newarray(...)`, блокируется до игры с `runtime-persistent-array-use-without-newarray`.
- Повторное объявление локального имени в одном обработчике или функции, включая соседние ветви `if`, обнаруживается до запуска компилятора. Это предотвращает подтверждённое зависание RScript 4.10f без сообщения об ошибке.
- Разреженные или вышедшие за размер списка номера объектов RSON отклоняются до RScript с `rson-object-id-range` вместо внутреннего `List index out of bounds` или зависания.
- Точный пустой `Lang.dat` (`FF FE`) при декомпиляции распознаётся как отсутствие диалогов и не передаётся в зависающий этап импорта RScript; причина пропуска остаётся в JSON-отчёте.
- Тайм-ауты RScript стали адаптивными: небольшой проект сохраняет 60-секундное окно без прогресса, крупный получает больше времени по числу объектов, строк кода и размеру файла. Общий аварийный предел начинается с 600 секунд и также растёт; явное положительное значение остаётся пределом оператора, `0` отключает оба ограничения.
- Preflight RSON блокирует пятый `TGroup` для подтверждённого лимита RScript 4.10f, коллизии глобальных `DMsg.Num`/`AMsg.Num`, повторные имена `TDialog` и код, ошибочно помещённый в `TDialogAnswer.Msg`.
- Runtime-lint обнаруживает не зарегистрированные в графе persistent-переменные и повторные локальные объявления, включая списки `int a,b`. Объектный вызов после `&&`/`||` больше не считается защищённым: RScript не гарантирует безопасное короткое замыкание, поэтому guard и вызов должны быть разнесены по операторам.
- Проверяется доказанный сценарий зависания компилятора: диалоговый обработчик не может обращаться к persistent-массиву, объявленному позднее в графе. Сравнение старого и нового SCR/RSON отдельно выявляет новые массивы, спрятанные под уже пройденным first-run gate сохранения.
- Диалоговый анализ сверяет номера `DChange`/`DAdd`, цели `InjectAnswer`/`AddDialogInject`, достижимость обработчиков и риск `fastexit` на станции. Намеренный self-target динамического меню остаётся информационным сообщением, а не ложной ошибкой.
- Добавлены диагностики повторного использования ID Ether после `EtherDelete`, пояснение подтверждённого типа сообщения `8` и рекомендация установить `ShipStatistic(ship,10,home)` перед освобождением купленного через `BuyWarrior` корабля.
- `script decompile` возвращает структурированную диагностику ошибки импорта Lang.dat. Новый явный `--fallback-without-lang` позволяет продолжить без диалогов; автоматически и молча содержимое языка не отбрасывается.
- `script compare-scr` теперь включает сравнение persistent-схемы и смысловую карту диалогов. Полный набор из 172 тестов проверяет новые правила и прежние форматы.

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
| QM/QMM чтение, JSON-редактирование, сборка и аудит | да | — |
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

## Безопасность и границы

- `release build` создаёт staging-копию, проверяет архив повторным чтением и сверяет хэши.
- Ошибки блокируют релиз; предупреждения блокируются только с `--warnings-as-errors`.
- `unsupported` означает неполное покрытие, а не повреждение файла.
- GUI заблокирован по умолчанию и не нужен для штатных сценариев.
- `script validate` до запуска RScript ловит незакрытые строки/комментарии/скобки и случайный русский текст вне строки или комментария — известную причину зависания старого компилятора.
- `script decompile` управляет декомпилятором RScript только на изолированном невидимом desktop, не изменяет исходный SCR, выдаёт поэтапный JSON и публикует RSON лишь после цикла `SCR → RSON → SCR`.
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

В версии 0.9.3 полный набор из 172 тестов проверяет нативные PNG/GI,
лексический preflight, прогресс-зависимые таймауты, fail-closed декомпиляцию, глубокий
round-trip, сравнение SCR и завершение скрытого дерева процессов при обрыве
агента, а также QM/QMM reader/writer, формулы квестов и JSON-цикл. На локальном корпусе глубоко проверено 3098 из 3131
GI; оставшиеся 33 корректно классифицированы как `unsupported`. Дополнительно
dev-аудит был выполнен на 421 установленном моде без падений валидаторов.
Он выполнил 3790 проверок и распознал 48 QM/QMM.

## Авторство

SRHD XenoModKit создан и поддерживается **Xenomorphchyma**. Сторонние кодеки и форматы принадлежат их соответствующим авторам; они перечислены отдельно и не присваиваются проекту.
