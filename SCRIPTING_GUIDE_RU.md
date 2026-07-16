# Скрипты Space Rangers HD без GUI

## Что хранится в RSON

Проект RScript — обычный JSON с обязательными полями `FileID`, `FileVersion`,
`ScriptName` и объектом `Visual`. В `Visual.Objects` находится граф объектов, а
в `Visual.Links` — связи между ними. Актуальные изученные проекты используют
`FileID=573785173` и `FileVersion=8`.

Каждый объект имеет уникальный числовой `#`. Поле `Parent`, а также `Begin` и
`End` у связей должны ссылаться на существующие номера. ModKit проверяет эти
инварианты до компиляции. Среди реально встречающихся типов: `TState`, `TGroup`,
`TDialog`, `TDialogMsg`, `TDialogAnswer`, `TVar`, `Twhile`, `TStar`, `TPlanet`,
`TStarShip`, `TItem`, `TPlace`, `Tif`, `TGraphLink` и другие.

### Операции над графом без GUI

ModKit 0.8.0 не конструирует неизвестные типы объектов из догадок. Вместо этого
`clone-object` копирует реальную, уже принятую RScript структуру в той же группе,
выдаёт новый уникальный `#` и позволяет сменить имя. Связи создаются в реально
наблюдаемой форме `TGraphLink`:

```powershell
python srhd.py script clone-object D:\work\Script.rson D:\work\Script.clone.rson `
  --id 12 --name "Новая операция"
python srhd.py script add-link D:\work\Script.clone.rson D:\work\Script.link.rson `
  --begin 12 --end 13 --nom 0
python srhd.py script list-links D:\work\Script.link.rson
python srhd.py script delete-link D:\work\Script.link.rson D:\work\Script.unlinked.rson `
  --index 0
```

`delete-object` по умолчанию отказывается удалять объект, если на него указывают
`Begin`, `End` или `Parent`. Явный `--detach-references` удаляет относящиеся к
объекту связи и ставит его непосредственным детям `Parent=-1`; сами дети не
стираются. Каждый выходной RSON заново проходит полную проверку графа.

```powershell
python srhd.py script delete-object D:\work\Script.rson D:\work\Script.deleted.rson `
  --id 13 --detach-references
```

## Где находится код

Код хранится массивами строк в полях `Code`, `ActCode` и `LinkCode`. Синтаксис
C-подобный; в существующих модах встречаются вызовы функций, условия, циклы,
присваивания и комментарии `//`/`/* ... */`. Точный набор доступных функций
задаёт среда игры и её справочник RScript — библиотека не выдумывает новые API.

### События состояния без GUI

Подписки состояния `TState` хранятся не в `EUnique`, `EMsg` или `OnTalk`.
RScript 4.10f ожидает специальную первую строку поля `OnActCode`, после которой
идёт обычный код обработчика:

```text
[t_OnEnteringForm,t_OnPlayerBuyEq|]
PlayerActCode();
```

ModKit 0.8.0 умеет безопасно менять эту сигнатуру, не затирая обработчик:

```powershell
python srhd.py script set-events D:\work\MyScript.rson D:\work\MyScript.events.rson `
  --id 2 --event t_OnEnteringForm --event t_OnPlayerBuyEq
python srhd.py script set-events D:\work\MyScript.events.rson D:\work\MyScript.noevents.rson `
  --id 2 --clear
```

`script validate` проверяет синтаксис сигнатуры, `script info` показывает
подписки TState, а `script inspect-scr` подтверждает сигнатуры в собранном SCR.
Формат проверен интеграционной сборкой через RScript CLI; редактор не требуется.

## Смысловой runtime-lint

Успешная компиляция подтверждает синтаксис, но не правильность игрового объекта
или момента выполнения. Поэтому ModKit до запуска RScript строит локальный граф
вызовов функций и проверяет точки `Turn`, `TState.OnActCode`, глобальную
инициализацию и стартовые `ScriptRun` в BlockPar.

```powershell
python srhd.py script lint-runtime D:\work\MyMod
python srhd.py script lint-runtime D:\work\MyMod --strict
```

Для отдельного проекта связанные артефакты можно указать явно:

```powershell
python srhd.py script lint-runtime D:\work\MyScript.rson `
  --main D:\work\CFG\Main.dat `
  --module-info D:\work\ModuleInfo.txt
```

### Безопасный запуск в контексте игрока

Если первым аргументом служит звезда игрока, планета должна соответствовать его
фактическому местонахождению:

```text
ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'Mod_Name');
```

Форма ниже блокируется кодом `runtime-unsafe-player-planet-context`, поскольку
первая планета звезды не обязана быть планетой игрока:

```text
ScriptRun(ShipStar(Player()), StarPlanets(ShipStar(Player()), 0), 'Mod_Name');
```

### Барьер до первого интерфейсного события

Код, достижимый из `Code.Type=Turn`, не должен обращаться к
`Player`, магазинам, хранилищам или сканированию галактики, пока обработчик
`t_OnEnteringForm` не подтвердил готовность мира. Проверенный шаблон:

```text
// Top с Code.Type=Global
runtime_ready = 0;
runtime_ready_turn = 0;

// Top с Code.Type=Turn — guard и работа находятся в этом же Code object
if(!runtime_ready || CurTurn() <= runtime_ready_turn) exit;
planet = GetShipPlanet(Player());
// дальнейшая работа Turn находится здесь же

// TState.OnActCode с t_OnEnteringForm/t_OnPlayerBuyEq
if(ScriptItemActionType(t_OnEnteringForm)) exit;
if(!ScriptItemActionType(t_OnPlayerBuyEq)) exit;
if(!runtime_ready)
{
    runtime_ready = 1;
    runtime_ready_turn = CurTurn();
}
```

RScript не связывает пользовательские функции между разными code object.
Конструкция ниже блокируется как
`runtime-cross-block-function-call`, потому что иначе SCR может собраться, но
игра на первом ходе выдаст `Not link var :Mod_Turn`:

```text
// Global Top
function Mod_Turn() { ... }

// другой Top, Code.Type=Turn
Mod_Turn();
```

Флаг обязан быть изначально нулевым и устанавливаться только в обработчике
`TState` с `t_OnEnteringForm` (он может одновременно обслуживать
`t_OnPlayerBuyEq`). Inline-guard в начале Turn распознаётся как полноценный
барьер. Для цепочки отдельных объектов визуального графа используйте корневое
`Tif`-условие `CurTurn() > 0`: его истинная ветвь `Nom=0` передаёт доказанный
барьер всем downstream-объектам. При слиянии путей доказательство сохраняется
только тогда, когда защищены все входящие пути; альтернативная прямая связь
снова включает предупреждение.

Runtime code object нельзя связывать с пользовательскими переменными другого
Top или обработчика. Ограничение относится к `Turn`, `Tif`, `ActCode`,
`LinkCode` и `OnActCode`. Например,
`runtime_ready && CurTurn() > runtime_ready_turn` в отдельном `Tif`
компилируется, но в игре даёт `Not link var :runtime_ready_turn`; ModKit
блокирует это как `runtime-cross-block-variable-reference`. Встроенное
`CurTurn() > 0` не требует межблочной переменной и является подтверждённым
барьером генерационного хода.

Связанный runtime-объект не должен содержать пустой массив `Code=[]`,
`ActCode=[]` или `LinkCode=[]`. Подтверждённый probe из десяти строк с пустым
downstream Top зависал в RScript 4.10f, тогда как уникальные исполняемые no-op
строки собирались за несколько секунд. Перед компиляцией ModKit блокирует такой
граф как `runtime-linked-empty-code`. Изолированный несвязанный шаблон редактора
не считается активной runtime-ветвью.
Дополнительная проверка ловит рекурсивные циклы вызовов и
буквально неограниченные циклы `while(1)`/`for(;;)`.

Проверяется и суммарная вложенность циклов по графу вызовов. Например, цепочка
`Turn → цикл по звёздам → цикл по планетам → цикл по типам → цикл по предметам`
блокируется как `runtime-nested-world-loop`, даже если каждый отдельный цикл
формально конечен. Такой обход нужно превращать в очередь/курсор и обрабатывать
небольшое фиксированное число объектов за один ход.

Прямой доступ из маленького `Turn` без известного способа запуска считается
предупреждением. Если тот же мод запускается из `BV/OnStart`, полный аудит
повышает его до блокирующей ошибки. Это уменьшает ложные срабатывания для старых
скриптов, запускаемых только после загрузки интерфейса.

Строки в двойных кавычках компилятор связывает с языковым файлом. Поэтому при
сборке нужно хранить вместе полученные `.scr` и файл Lang, а исходный `.rson`
нужно сохранять всегда. Из одного скомпилированного SCR полноценный визуальный
RSON-проект надёжно восстановить нельзя: документированный CLI RScript не имеет
операции SCR→RSON.

Если скрипт не создаёт языковых строк, RScript записывает
`DATA/Script/Lang.dat` как два байта `FF FE` — пустой UTF-16LE с BOM. Это
корректный специальный формат, а не повреждённый BlockPar. Команда
`dat validate` распознаёт его без запуска BlockParEditor; исключение действует
только для точного пути `DATA/Script/Lang.dat` и точного содержимого `FF FE`.

## Сборка

```powershell
cd D:\SRHD_Modding\Tools\SRHDModKit
python srhd.py script validate D:\work\MyScript.rson
python srhd.py script build D:\work\MyScript.rson `
  --scr D:\work\MyScript.scr --lang D:\work\MyScript.txt
```

Если RSON находится внутри мода с `ModuleInfo.txt`, `script build` перед
компилятором автоматически проверяет весь корень мода, включая оба `Main`.
Поэтому исправленный RSON нельзя случайно собрать вместе со старым опасным
`CFG/Main.dat`.

Если присутствует `CacheData`, сборка также требует согласованной цепочки:

```text
DATA/Script/Mod_Name.scr
Data/Script: Mod_Name=1,Script.Mod_Name
CacheData Script: Mod_Name=Mods\<раздел>\<папка мода>\DATA\Script\Mod_Name.scr
```

Проверяются и `SOURCE/CFG/CacheData.txt`, и `CFG/CacheData.dat`. Их семантическое
расхождение блокируется кодом `cachedata-source-binary-mismatch`; неверное имя
или путь — `cache-script-key-path-mismatch` и
`cache-script-local-path-mismatch`; отсутствующая локальная запись —
`cache-script-missing`. Дополнительная ссылка на SCR другого мода допустима,
если она не подменяет локальный зарегистрированный SCR: так работают некоторые
патчи зависимостей с узлом `Script ~{`.

Русская полезная нагрузка `CFG/Rus/*.dat` должна расшифровываться как
Windows-1251. UTF-8 здесь опасен: редактор способен без потерь выполнить
`DAT → TXT → DAT`, но игра прочитает те же байты как CP1251 и покажет mojibake.
ModKit проверяет итоговый DAT, исходный `Lang_Rus.txt`, признаки `РџС...`/`Ð...`
и представимость каждого символа в CP1251. `ModuleInfo.txt` имеет отдельное
правило: разрешены используемые штатными модами UTF-16LE/BE и CP1251.

Выходной SCR проверяется по версии. RScript 4.10f работает с версиями 6, 7 и 8.
Компилятор запускается на невидимом рабочем столе Windows: его служебное модальное
окно не появляется в пользовательском сеансе и перехват управления не нужен.

## Подключение скрипта в моде

Скомпилированный файл размещается в `DATA/Script/Name.scr`. Регистрация находится
в блоке `Data/Script` файла `CFG/Main.dat`. В изученных модах встречается форма:

```text
Data ^{
    Script ^{
        Name=1,Script.Name
    }
}
```

Также встречается первый флаг `0`; его смысл зависит от сценария, поэтому ModKit
не заменяет его автоматически. Для запуска в начале игры существующие моды часто
добавляют вызов `ScriptRun(...)` в `BV/OnStart/0DayScripts`, но это наблюдаемый
шаблон, а не универсальное требование для каждого скрипта.

Регистрацию можно выполнить без редактора. Стартовый `ScriptRun(...)` библиотека
не угадывает: если он нужен, вызывающий передаёт проверенный код явно.

```powershell
python srhd.py script register D:\work\CFG\Main.dat D:\out\CFG\Main.dat `
  --name Mod_MyScript --flag 1

python srhd.py script register D:\work\CFG\Main.dat D:\out\CFG\Main.dat `
  --name Mod_MyScript --flag 1 `
  --startup-key 90_MyScript `
  --startup-code "ScriptRun(ShipStar(Player()), GetShipPlanet(Player()), 'Mod_MyScript');"
```

Команда аудита проверяет наличие SCR, его версию, `CFG/Main.dat`, блок регистрации
и полноценные исходники RSON:

```powershell
python srhd.py script audit-mod D:\path\MyMod
```

## Подтверждённые API стоимости оборудования

Локальный справочник RScript и скомпилированные установленные моды подтверждают
следующие формы:

```text
current = RangersCapital();
cost = ItemCost(item);
ItemCost(item, new_cost);

module = EqModule(item);
ModuleToEquipment(-1, item);
ModuleToEquipment(module, item);

count = ItemExtraSpecialsCountByType(item, bonus);
ItemExtraSpecialsAddByType(item, bonus, count);
ItemExtraSpecialsDeleteByType(item, bonus, count);
```

`ItemCost(item, value)` используется реальными модами для прямого изменения
цены. Временное снятие и восстановление `EqModule` также является существующим
шаблоном и позволяет менять базовую стоимость, не уничтожая обычный
микромодуль. Extra-special с `Special=1`, `NonSearchable=1` и пустым `ExText`
может служить невидимой сохраняемой меткой предмета. Третий аргумент
`AddByType`/`DeleteByType` — количество экземпляров, а не произвольное числовое
поле. Реальные моды подтверждают безопасное использование малых количеств. Не
следует передавать туда цену или другой большой `int`: это способно раздуть
предмет тысячами или миллионами extra-special и заблокировать создание
галактики или сохранения.

Все перечисленные вызовы дополнительно приняты компилятором RScript 4.10f в
исходниках EIDM v1. Полное поведение сохранения значений всё равно должно быть
проверено в игре на отдельном тестовом сохранении. Для сохранения целого числа
EIDM v1.2 кодирует его десятичными метками с количеством 0–9 на разряд.

### Разделение технического роста и инфляции

В установленном `Mod_EvoFreeInflation` подтверждены доступные через
`GetValueFromScript` глобальные значения:

```text
start_capital_amount
cur_capital_amount
max_reached_GTL
```

Его полный индекс капитала состоит из денежного роста по `CurTurn()` и
технического множителя:

```text
PortionInDiapason(max_reached_GTL, 1, 8, 1.0, 4.0)
```

Поэтому мод, который использует фактическую техническую стоимость оборудования,
а затем начисляет инфляцию, не должен повторно применять этот компонент. В EIDM
v1.2 денежный коэффициент при активном `EvoFreeInflation` рассчитывается как:

```text
(cur_capital_amount / start_capital_amount)
/
PortionInDiapason(max_reached_GTL, 1, 8, 1.0, 4.0)
```

Исходный `ItemCost` остаётся авторитетной технической ценой: игровые классы
оборудования используют разные характеристики, а оружие — отдельные `kCost`,
поэтому одна вручную заданная таблица абсолютных цен уничтожила бы различия и
совместимость с модовым оружием. EIDM v1.3 оставляет фактический `ItemCost`
авторитетной базой, а индекс `100→400%` применяет только для отделения
технологической части капитала EvoFreeInflation.

## Состояние EIDM

Старая папка EIDM и файл
`SOURCE/Mod_EIDM_reference_not_rscript_project.rson` сохранены как legacy.
Каноническая реализация находится в `Projects/EIDM/v1` и содержит два настоящих
проекта RSON версии 8:

- `EIDM_EquipmentInflationDynamicMarket` — основной мод;
- `EIDM_EquipmentInflationCleanup` — восстановление базовых цен перед удалением.

Оба проекта проходят `script validate`, собираются RScript 4.10f, содержат
SCR в `DATA/Script`, регистрацию в `CFG/Main.dat` и исходные BlockPar-тексты
в `SOURCE/CFG`. Версия v1.3 хранит применённый коэффициент в десяти невидимых
метках V3, по одной десятичной цифре 0–9 на разряд. Это заменило небезопасную
схему v1.1, которая записывала большие значения как количество extra-special.
Метки V1/V2 сохранены в CFG только для миграции и cleanup.

Начиная с v1.5 удалённые магазины обрабатываются пошаговыми курсорами раз в пять
ходов. За один запуск выполняются 12 независимых локальных и 12 удалённых шагов; между
пакетами предметы не сканируются. Вызовы развёрнуты в коде, поэтому глубина циклов одного
шага не складывается с глубиной следующего. Вложенных обходов звёзд, планет, магазинов и предметов
нет. Запуск использует `GetShipPlanet(Player())`, а обработка мира
вообще не вооружается от общего `t_OnEnteringForm`: она ждёт явной
`t_OnPlayerBuyEq` и следующего хода. Оба проекта обязаны проходить
`script lint-runtime --strict`; это исключает выполнение мутаций `ItemCost`
во время генерации галактики и блокирующие цепочки вложенных циклов.

Журнал игры подтвердил, что пользовательская функция, объявленная в одном
RSON code object и вызванная из другого, приводит к `Not link var` на нулевом
ходу. Поэтому ModKit 0.8.0 блокирует такие связи как
`runtime-cross-block-function-call`. Функция, объявленная и вызванная внутри
одного `Top.Code`, допустима и нужна для двух особенностей RScript 4.10f:
`StarPlanets`/`StarRuins` и цикл по магазину могут подвешивать компилятор в
голом Top-коде, но штатно компилируются в локальной функции того же узла.
Журнал также подтвердил, что отдельные downstream `Top` не связывают переменные
предыдущих узлов: после безопасного `CurTurn() > 0` игра остановилась на
`Not link var :eidm_cycle_valid`. Поэтому передаваемое состояние и его обработка
должны находиться в одном Turn object. Корневой `CurTurn() > 0` может оставаться
отдельным `Tif`: он не использует пользовательских переменных, а его истинная
ветвь переносит generation barrier downstream только тогда, когда все входящие
пути защищены.

Все операции редактирования кода, подписок, сборки SCR, кодирования DAT, аудита и
упаковки выполняются командами `srhd.py` без GUI. Для EIDM v1.5 расширение
публичного Python API не потребовалось.
