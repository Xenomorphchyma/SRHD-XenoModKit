# Внешние инструменты SRHD XenoModKit

Python-ядро ModKit не имеет обязательных пакетных зависимостей. Внешние EXE
нужны для полного `DAT ↔ TXT`, `RSON/SCR`, модульного `RSM → SCR` и
legacy-конвертации `RSON ↔ SVR`. `GI ↔ PNG`, `GAI/PKG` и текстовые квесты
`QM/QMM` обрабатываются собственными Python-кодеками.

## Автоматическая установка

Из корня репозитория:

```powershell
.\scripts\setup-tools.ps1
python -B srhd.py tools
```

Установщик:

- не требует прав администратора и не меняет игру или `ModCFG.txt`;
- загружает официальные BlockParEditor 2.1, RScript 4.15f и rsmc, а также
  зафиксированную 4.10f для legacy-конвертации SVR;
- проверяет SHA-256 каждого архива и основного EXE;
- не перезаписывает неизвестные файлы;
- при обнаружении точно проверенных 1.9/4.10f переносит их в
  `BlockParEditor19/` и `RScript410/`, затем устанавливает новые штатные версии.

По умолчанию инструменты размещаются рядом с клоном. Альтернативный корень
задаётся `-ToolsRoot` установщику и `--tools-root` командам ModKit.

## BlockParEditor 2.1

Нужен для кодирования и декодирования игровых DAT.

- официальный релиз: <https://github.com/indiemagpie/BlockParEditor/releases/tag/2.1>
- архив: <https://github.com/indiemagpie/BlockParEditor/releases/download/2.1/BlockParEditor_2.1.zip>
- ZIP SHA-256: `D0C4739F9FBF1C276AC6979C13E838B217694AC16400109DB7ED53435F0D0253`
- EXE SHA-256: `84D1FA626451F165135139EC9D00306207BA765B2CA3071591009F831B5711FA`

Ожидаемый файл:

```text
<TOOLS_ROOT>/BlockParEditor/BlockParEditor.exe
```

В 2.1 DLL отсутствует и не требуется. ModKit определяет версию по Windows
VERSIONINFO и запускает оригинальный EXE напрямую. Для сохранённой 1.9 при
необходимости создаётся производная `BlockParEditor.Legacy.exe` с локальным
manifest `activeCodePage=ru-RU`; исходный EXE и системная кодовая страница не
меняются.

Обе проверенные версии могут выдавать различный бинарный DAT при одинаковом
содержимом. Поэтому ModKit никогда не доказывает корректность одним SHA-256:
после каждой сборки выполняется `TXT → DAT → TXT`, а сравнивается каноническое
дерево BlockPar. Входные TXT протестированы в UTF-8, CP1251 и UTF-16; игровой
текст всё равно обязан быть представим в Windows-1251. Последовательность `//`
в значении, включая `https://`, блокируется заранее — редактор считает её
началом комментария и обрезает значение.

## RScript 4.15f

Штатный компилятор/декомпилятор RSON и экспортёр модульного RSM.

- официальный релиз: <https://github.com/indiemagpie/RScript/releases/tag/4.15f>
- исходный проект и Example: <https://github.com/indiemagpie/RScript/tree/main>
- архив: <https://github.com/indiemagpie/RScript/releases/download/4.15f/RScript_4.15f.zip>
- ZIP SHA-256: `DF39FF7D00242812EDD543899F5427DFC25B0D0B37032982B7673536AD5E7B40`
- EXE SHA-256: `1CF7724E37657E00103652D4843C8A4D3DCFEEDAA533703CE8EA82209252693F`

Архив извлекается целиком в `<TOOLS_ROOT>/RScript/`. ModKit определяет
VERSIONINFO и использует новый настоящий CLI:

```text
RScript.exe --cli -b source.rson output.scr output.lang.txt --full
RScript.exe --cli -d source.scr output.rson [--langdat Lang.dat]
RScript.exe --cli -x source.rson output.rsm [--split]
```

Видимая GUI-автоматизация для 4.15f не используется. Процесс всё равно
запускается в управляемом Job Object, остаётся видимым в Диспетчере задач и
гарантированно завершается вместе с операцией ModKit.

## rsmc из RScript 4.15f

rsmc компилирует один или несколько текстовых `.rsm` в SCR.

- архив: <https://github.com/indiemagpie/RScript/releases/download/4.15f/rsmc.zip>
- ZIP SHA-256: `FDE03859AA2DB3E15514E3DF9A5044D79EC0D1D684414B6105AED9C1ED1A7E6A`
- EXE SHA-256: `BFB59C8EDA31BDACF2865DD8179E8278331215E51D4297AF6325566599427CAA`
- ожидаемый файл: `<TOOLS_ROOT>/RSMCompiler/rsmc.exe`

`rsmc --lang-txt` и `--lang-dat` не создают язык с нуля: перед запуском файл
уже должен существовать и содержать `Script/<ScriptName>`. Команды ModKit
создают staging-базу автоматически, затем проверяют языковое дерево,
скомпилированный SCR, runtime-lint и круговой проход декомпиляции. Сырой вывод
rsmc никогда не считается готовым релизом.

## Совместимость с RScript 4.10f

Если основным EXE обнаружена именно 4.10f, сборка RSON и декомпиляция SCR
остаются поддержанными. Декомпиляция выполняется старым скрытым бэкендом на
изолированном desktop, потому что настоящий CLI в этой версии отсутствует.
Сообщение `tools` явно показывает `legacy-cli`.

При обновлении setup сохраняет проверенную 4.10f в
`<TOOLS_ROOT>/RScript410/`, а при чистой установке загружает её из
зафиксированного архивного источника. RScript 4.15f удалил CLI-конвертацию SVR,
поэтому только эта версия используется для явной команды `script convert`
между RSON и SVR. Если legacy-каталога нет, ModKit выдаёт понятную ошибку и не
пытается открыть GUI.

Проверенный EXE 4.10f SHA-256:
`B6E6A0E809EC65215E0C72F58CC9C2707E6F29F56BB625B162523C89489A7777`.
Архив SHA-256:
`E98E2EBD9102D648C744DCB40DA04FC94B00C133BA7C2DF86F58F5AA04C35850`.

## Необязательные GUI-инструменты

TGE не нужен для QM/QMM. ResEditor и ShipViewer не нужны headless-библиотеке и
автоматическому выпуску модов. ModKit знает их только для явно разрешённой
ручной команды `open`, которая по умолчанию заблокирована.

Все права на сторонние инструменты принадлежат их авторам. SRHD XenoModKit
фиксирует происхождение и хэши, но не присваивает их авторство. Сведения о
сторонних исследованиях форматов находятся в
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
