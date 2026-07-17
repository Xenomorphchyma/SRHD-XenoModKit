# Внешние инструменты SRHD XenoModKit

Python-ядро ModKit не имеет обязательных пакетных зависимостей. Внешние EXE нужны только для полного `DAT ↔ TXT`, декомпиляции `SCR → RSON` и сборки `RSON/SVR → SCR`. Преобразование `GI ↔ PNG` и работа с текстовыми квестами `QM/QMM` выполняются собственными Python-кодеками ModKit.

## Автоматическая установка

Из корня репозитория:

```powershell
.\scripts\setup-tools.ps1
python -B srhd.py tools
```

Установщик:

- не требует прав администратора;
- не изменяет игру и `ModCFG.txt`;
- загружает только BlockParEditor 1.9 и RScript 4.10f;
- проверяет SHA-256 архива и основного EXE до публикации файлов;
- отказывается перезаписывать существующую папку инструмента.

По умолчанию инструменты размещаются рядом с клоном. Альтернативный корень задаётся `-ToolsRoot` установщику и `--tools-root` командам ModKit.

## BlockParEditor 1.9

Нужен для полного чтения, изменения, кодирования и обратной проверки игровых DAT.

- описание: <https://rangers.fandom.com/ru/wiki/BlockParEditor>
- исходный проект: <https://github.com/indiemagpie/BlockParEditor>
- архив 1.9: <https://web.archive.org/web/20251227105508id_/https://vertix.games/tools/BlockParEditor_1.9.zip>
- ZIP SHA-256: `E1D570C007B6999EE18BFA1D273F59986771FE8BD2863C38487F470FF69030E5`
- EXE SHA-256: `414A289E9F87C4088AD27D79F20A5206D03ACA9124E89D6767D0A042CD794D4F`
- DLL SHA-256: `BE469C7EAF987CD317CC378C47829B282A17BCA4917D3FC5DA0A7675711DC723`

Ожидаемая структура:

```text
<TOOLS_ROOT>/BlockParEditor/BlockParEditor.exe
<TOOLS_ROOT>/BlockParEditor/BlockParEditor.dll
<TOOLS_ROOT>/BlockParEditor/cli.txt
```

При первом запуске ModKit создаёт рядом производную копию `BlockParEditor.Legacy.exe` с локальным manifest `activeCodePage=ru-RU`. Исходный EXE и системная кодовая страница Windows не меняются.

## RScript 4.10f

Нужен для декомпиляции `SCR → RSON`, сборки `RSON/SVR → SCR` и конвертации
RSON/SVR.

- описание: <https://rangers.fandom.com/ru/wiki/RScript>
- исходный проект: <https://github.com/indiemagpie/RScript>
- архив 4.10f: <https://web.archive.org/web/20251227105421id_/https://vertix.games/tools/RScript_4.10f.zip>
- ZIP SHA-256: `E98E2EBD9102D648C744DCB40DA04FC94B00C133BA7C2DF86F58F5AA04C35850`
- EXE SHA-256: `B6E6A0E809EC65215E0C72F58CC9C2707E6F29F56BB625B162523C89489A7777`

Архив нужно извлечь целиком:

```text
<TOOLS_ROOT>/RScript/RScript.exe
<TOOLS_ROOT>/RScript/cfg.txt
<TOOLS_ROOT>/RScript/BlockPar/...
<TOOLS_ROOT>/RScript/Icons/...
<TOOLS_ROOT>/RScript/Schemes/...
```

ModKit запускает старую GUI-subsystem программу на отдельном невидимом desktop,
ждёт выходные файлы и завершает процесс. Видимый GUI для декомпиляции и сборки
не требуется.

## Необязательные GUI-инструменты

TGE не нужен для чтения, JSON-редактирования, проверки и сборки QM/QMM.
ResEditor и ShipViewer не нужны headless-библиотеке и автоматическому выпуску модов.
ModKit знает GUI-инструменты только для явно разрешённой ручной команды `open`,
которая по умолчанию заблокирована. Автоматический агент не должен устанавливать
или запускать их.

Все права на сторонние инструменты принадлежат их авторам. SRHD XenoModKit фиксирует происхождение и хэши, но не присваивает их авторство. Сведения о сторонних проектах, использованных при исследовании форматов, находятся в [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
