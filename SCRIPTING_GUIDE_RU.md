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

ModKit 0.8.5 не конструирует неизвестные типы объектов из догадок. Вместо этого
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

`script validate` до запуска RScript проверяет незакрытые строки, блочные
комментарии и пары `()[]{}`. Русский текст допустим внутри строки или
комментария, но фраза после оператора без `//`, например
`q=0;Потерянный комментарий`, блокируется как `rscript-uncommented-text`.
Такой дефект способен не выдать нормальную ошибку, а повесить RScript 4.10f.

### События состояния без GUI

Подписки состояния `TState` хранятся не в `EUnique`, `EMsg` или `OnTalk`.
RScript 4.10f ожидает специальную первую строку поля `OnActCode`, после которой
идёт обычный код обработчика:

```text
[t_OnEnteringForm,t_OnPlayerBuyEq|]
PlayerActCode();
```

ModKit 0.8.5 умеет безопасно менять эту сигнатуру, не затирая обработчик:

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

RScript не связывает пользовательские функции между произвольными code object.
Конструкция ниже блокируется как
`runtime-cross-block-function-call`, потому что иначе SCR может собраться, но
игра на первом ходе выдаст `Not link var :Mod_Turn`:

```text
// Global Top
function Mod_Turn() { ... }

// другой Top, Code.Type=Turn
Mod_Turn();
```

Подтверждённое исключение — функции Top с явным `Code.Type=Init`: восстановленные
проекты RScript используют этот объект как общую библиотеку для Turn. Аналогично
`TVar` является общей переменной проекта и не считается несвязанным локальным
присваиванием. Top без явного `Init` такого исключения не получает.

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

### Время жизни Item между ходами

`Item` — скриптовый объект движка, а не устойчивый числовой указатель. Сохранение
результата `CreateQuestItem` как обычного `dword` в общем `TVar` или массиве
может оставить адрес уже освобождённого объекта на следующем ходу. Вызов
`ItemExist` не делает такой адрес безопасным: само разыменование неверного типа
может завершиться `EAccessViolation`.

Небезопасный шаблон блокируется как `runtime-persistent-raw-item-handle`:

```text
dword cargo = CreateQuestItem(...);
ArrayAdd(cargo_registry, cargo);
```

Храните стабильный ID и восстанавливайте объект только на текущем ходу:

```text
dword cargo = CreateQuestItem(...);
ArrayAdd(cargo_registry, Id(cargo));

dword current_cargo = IdToItem(cargo_registry[cursor]);
if(current_cargo) ItemExist(current_cargo);
```

Анализ межпроцедурный: передача сырого `cargo` в функцию-сеттер, которая пишет
параметр в общий `TVar` или `ArrayAdd`, также считается ошибкой.

### Ссылки на Planet, Star и Ship в сохранениях

Скриптовые ссылки на объекты мира также не являются устойчивыми идентификаторами.
После обновления SCR старое сохранение может оставить в общем `TVar` уже
недействительную `Planet`, `Star` или `Ship`; первый `PlanetToStar`, `StarOwner`
или другой вызов, разыменовывающий такую ссылку, способен завершиться
`EAccessViolation`.

`runtime-persistent-world-object-handle` требует для долгоживущего состояния:

1. хранить отдельный общий числовой ID через `Id(object)`;
2. при миграции сначала безусловно присвоить старой ссылке `0`;
3. условно восстановить текущий объект через `IdToPlanet`, `IdToStar` или
   `IdToShip`;
4. вызывать функцию восстановления из исполняемого кода RSON до использования
   ссылки.

```text
function RestoreWorldRefs()
{
    destination = 0;
    target_star = 0;
    if(destination_id) destination = IdToPlanet(destination_id);
    if(target_star_id) target_star = IdToStar(target_star_id);
}

RestoreWorldRefs();
destination_id = Id(destination);
target_star_id = Id(target_star);
```

Проверка распространяет типы аргументов через локальные вспомогательные функции.
При этом общий `TVar`, который доказуемо получает свежий объект непосредственно
перед использованием в том же вызове обработчика, распознаётся как временная
рабочая переменная и не требует отдельного ID.

Графовый `TItem` не является заменой динамическому предмету. По подтверждённому
round-trip RScript объект должен находиться именно в коллекции `Items` и иметь
строковое поле `+Place`; пустая строка допустима. Отсутствие буквального
`Items.Count` само по себе не ошибка: рабочие декомпилированные RSON его не
сериализуют. Если поле всё же присутствует, оно обязано совпадать с числом
`TItem`. Нарушения блокируются кодами `rson-titem-collection`,
`rson-titem-place`, `rson-items-collection` и `rson-items-count` до запуска
старого компилятора.

### Безопасная разгрузка и удаление кораблей

`OrderTakeOff` не переводит посаженный корабль в нормальный космос мгновенно.
После `GetItemFromShip`/разгрузки нельзя вызвать `ShipOut` для того же корабля в
том же прямом пути обработчика. Безопасная последовательность занимает минимум
два хода:

1. выгрузить и освободить груз;
2. пометить транспорт доставленным, выполнить `OrderTakeOff` и завершить
   обработчик;
3. на следующем ходу подтвердить отсутствие `GetShipPlanet`/`GetShipRuins` и
   `!ShipInHyperSpace`, затем выполнить `ShipOut`.

Связка отслеживается через локальный граф функций и блокируется как
`runtime-landed-shipout-after-mutation`, даже если разгрузка скрыта в нескольких
вспомогательных функциях.

Если `ShipOut` удаляет текущий `GroupShip` во время прямого обхода от нуля до
`GroupCount`, состав и индексы группы меняются. Код
`runtime-group-mutated-during-iteration` требует отдельный обратный обход.
Уменьшение курсора после удаления не помогает: условие прямого цикла всё равно
снова вызывает `GroupCount` уже после повреждения итератора. После прохода нужно
закончить обработчик и продолжить работу на следующем ходу. Повторный
`GroupCount` той же группы после обратного прохода без промежуточного
`exit`/`return` блокируется отдельно как
`runtime-group-recount-after-mutation`.

Дополнительная проверка ловит рекурсивные циклы вызовов и
буквально неограниченные циклы `while(1)`/`for(;;)`.
Одношаговая нормализация параметров распознаётся как ограниченная только когда
ModKit доказывает, что все самовызовы находятся под проверкой нулевой суммы и
передают в неё ненулевые числовые константы. Остальная рекурсия блокируется.

Объекты `Code.Type=Turn`, входящие из `TDialog`, `TDialogMsg`, `TDialogAnswer`
или `DialogBegin`, относятся к диалоговой ветви и не считаются периодическим
игровым ходом. Это не отключает остальные проверки их кода.

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
сборке нужно хранить вместе полученные `.scr`, файл Lang и исходный `.rson`.
Если исходник всё же потерян, ModKit 0.8.5 умеет автоматически вызвать
декомпилятор RScript 4.10f на изолированном невидимом desktop:

```powershell
python srhd.py script decompile D:\work\Mod_Name.scr D:\work\Mod_Name.rson `
  --lang-dat D:\work\Lang.dat --json
python srhd.py script compare-scr D:\work\Original.scr D:\work\Patched.scr --json
```

Документированный CLI самого RScript операции SCR→RSON не содержит, поэтому
ModKit безопасно автоматизирует встроенную форму без вывода окна пользователю.
Поддерживаются SCR версий 6, 7 и 8. Исходный SCR не изменяется; восстановленный
RSON проходит структурную проверку и обязательную повторную компиляцию. Числовой
суффикс подписок TState, например `[t_OnEnteringForm|0]`, распознаётся и
сохраняется. `Lang.dat` необязателен, но без него текст диалогов восстановить
полностью невозможно.

Отчёт декомпиляции содержит отдельные фазы и их длительность. Итоговый путь
получает файл только после успешного round-trip; непроверенный проект можно
оставить лишь отдельным явным `--keep-unverified`. `--deep-roundtrip` выполняет
ещё одно восстановление и проверяет стабильность версии, объектов, связей, строк
кода и типов. Для крупных модов лимиты адаптивны и не имеют верхнего потолка;
значение `0` у `--decompile-timeout`/`--roundtrip-timeout` полностью отключает
общий дедлайн.

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

`script build --timeout 0` отключает общий дедлайн компилятора. Без параметра
используется не фиксированное число, а неограниченно растущий расчёт по числу
объектов, строк кода и размеру RSON.

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

Все перечисленные вызовы приняты компилятором RScript 4.10f на локальных
проверочных проектах. Полное поведение объектов и сохранение значений всё равно
нужно проверять в игре на отдельном тестовом сохранении. Для хранения большого
целого числа в extra-special безопаснее использовать несколько малых разрядных
меток, чем записывать само число в параметр количества.

Все операции редактирования кода и подписок, декомпиляции и сборки SCR,
кодирования DAT, аудита и упаковки доступны через `srhd.py` без ручной работы в
GUI.
