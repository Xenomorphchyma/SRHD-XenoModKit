# Скрипты Space Rangers HD без GUI

## Что хранится в RSON

Проект RScript — обычный JSON с обязательными полями `FileID`, `FileVersion`,
`ScriptName` и объектом `Visual`. В `Visual.Objects` находится граф объектов, а
в `Visual.Links` — связи между ними. Актуальные изученные проекты используют
`FileID=573785173` и `FileVersion=8`.

Каждый объект имеет уникальный числовой `#`. Номера должны образовывать плотный
диапазон, начинающийся с `#0` или `#1`: большие разреженные ID приводят внутри
RScript к `List index out of bounds` или зависанию. Поле `Parent`, а также
`Begin` и `End` у связей должны ссылаться на существующие номера. ModKit
проверяет эти инварианты до компиляции. Среди реально встречающихся типов: `TState`, `TGroup`,
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

RScript также не создаёт отдельные области локальных имён для соседних ветвей
`if`. Повторное `dword selected` в двух ветвях одного обработчика может не дать
синтаксическую ошибку, а оставить компилятор в модальном цикле. Проверка
`runtime-duplicate-local-declaration` требует уникальное локальное имя во всей
функции или прямом коде обработчика; одинаковые имена в разных функциях
разрешены.

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

Кроме расположения уже найденных функций проверяется само существование каждого
вызова. `runtime-unresolved-user-function` сверяет имя с локальными объявлениями,
явными `ImportedFunction`/глобальными `TVar` и переносимым реестром API
SRHD 2.1.2500. Это важно, потому что RScript способен записать неизвестное имя в
SCR без ошибки, а игра остановит ход только позже с `Not link var :Name`.
Правило не использует префиксы конкретного мода.

Непарный ASCII-апостроф в `//`-комментарии блокируется как
`runtime-apostrophe-in-line-comment`: старый runtime-линкер способен продолжить
разбор такого комментария как незакрытой строки и «спрятать» последующее
объявление функции. Полностью закомментированный код с парными строковыми
литералами допускается. В обычном тексте используйте `’`, дефис или другую
формулировку.

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

### ABI динамических массивов RScript

Фиксированный `newarray(N)`, где `N > 1`, остаётся обычным массивом с индексами
`0..N-1`. Но динамический массив, созданный как `newarray(1)` и/или очищенный
`ArrayClear`, сохраняет служебный элемент `0` типа `vtUnknown`. Его реальные
записи лежат в `1..ArrayDim-1`, а пустой массив имеет `ArrayDim == 1`.

Поэтому runtime-lint блокирует:

- persistent `TVar`, переданный в `Array*` без единой доказанной инициализации
  `newarray(...)` — `runtime-persistent-array-use-without-newarray`;
- прямое чтение/запись `[0]` и циклы от `0`/до `>= 0` для доказанного
  динамического массива — `runtime-rscript-array-service-index`;
- проверки вроде `ArrayDim(queue) > 0` или `<= 0` —
  `runtime-rscript-array-empty-dimension`;
- общий индекс нескольких persistent-массивов без проверки равенства размеров —
  предупреждение `runtime-rscript-paired-array-dimension`.

Безопасный шаблон:

```text
unknown ids = newarray(1);
ArrayClear(ids);
if(ArrayDim(ids) <= 1) exit;
for(int i = 1; i < ArrayDim(ids); i = i + 1) Use(ids[i]);
```

### Тип anchor у UtilityFunctions.RndObject

Третий аргумент `RndObject(min, max, anchor)` не принимает `Item`. Если ModKit
доказывает происхождение значения от `CreateQuestItem`, `GetItemFromShip`,
`IdToItem` или другого Item-returning вызова, сборка блокируется как
`runtime-rndobject-anchor-type`. Используйте `Player`/`Ship`/`Planet`/`Star` как
подтверждённый anchor, а для независимого броска — встроенный `Rnd(min, max)`.

### Ссылки на Planet, Star и Ship в сохранениях

Скриптовые ссылки на объекты мира также не являются устойчивыми идентификаторами.
После обновления SCR старое сохранение может оставить в общем `TVar` уже
недействительную `Planet`, `Star` или `Ship`; первый `PlanetToStar`, `StarOwner`
или другой вызов, разыменовывающий такую ссылку, способен завершиться
`EAccessViolation`.

`runtime-persistent-world-object-handle` требует для долгоживущего состояния:

1. хранить отдельный общий числовой ID через `Id(object)`;
2. при миграции сначала безусловно присвоить старой ссылке `0`;
3. условно восстановить `Planet`/`Ship` через `IdToPlanet`/`IdToShip`, а `Star`
   — через локальный ограниченный обход `GalaxyStars()` и `GalaxyStar(i)`;
4. вызывать функцию восстановления из исполняемого кода RSON до использования
   ссылки.

```text
function IdToStar(int star_id)
{
    result = 0;
    if(!star_id) exit;
    for(int cursor = 0; cursor < GalaxyStars(); cursor = cursor + 1)
    {
        dword star = GalaxyStar(cursor);
        if(!star) continue;
        if(Id(star) == star_id)
        {
            result = star;
            exit;
        }
    }
}

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

`IdToStar` не является функцией игрового API SRHD 2.1.2500. RScript способен
записать неизвестное имя в SCR, но игра остановит ход с
`Not link var :IdToStar`. Поэтому голый вызов без локального определения
блокируется кодом `runtime-unsupported-engine-call`. Одноимённая локальная
функция считается восстановителем только когда ModKit доказывает полный
ограниченный проход от нулевого индекса, получение через `GalaxyStar(i)`,
сравнение `Id(star)` и нулевой результат при отсутствии объекта.

`IdToShip` существует, но его аргумент по справочнику движка обязан быть больше
`1`. В SRHD 2.1.2500 вызов `IdToShip(0)` способен вернуть не нулевой, а
непригодный указатель; последующий `ShipInScript`, `ShipStar` или другой доступ
завершает поток хода с `EAccessViolation`, что при создании галактики выглядит
как бесконечное «Проходит время». `runtime-id-to-ship-reserved-id` требует
явного доказательства перед вызовом:

```text
function RestoreShip(int ship_id)
{
    result = 0;
    if(ship_id <= 1) exit;
    dword ship = IdToShip(ship_id);
    if(ship) result = ship;
}
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

Третий аргумент `ShipJoin` — имя начального State. Нестроковое значение вроде
`1` не выбирает состояние, а явно отключает автоматический вход в него. Если
после этого NPC заблокировать через `OrderLock(ship, 1)` и не вызвать
`ChangeState`, военный корабль остаётся без корректного AI-состояния и способен
упасть в `TWarrior.NextDay`. `runtime-shipjoin-state-suppressed` блокирует этот
доказанный шаблон. Используйте обычный вход в первое состояние:

```text
ShipJoin(Escorts, ship);
OrderLock(ship, 1);
```

Либо передайте строковое имя состояния или сразу вызовите
`ChangeState('Escort', ship)`.

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

После `ShipOut`, `ShipDestroy`, `OrderTakeOff` или межпроцедурной разгрузки
корабля, полученного через `GroupShip`, нельзя снова читать связанную группу или
старый handle на том же живом пути. `runtime-post-group-mutation-dereference`
распространяет эффект через helper-функции и требует `exit`/`return`/`continue`
до следующего `GroupCount`, `GroupShip`, `GroupToShip` или разыменования.

Повторяющаяся цепочка
`GetItemFromShip → ReleaseItemFromScript → FreeItem` в одном вызове способна
повредить текущий `TStar.ScriptShipsAndItemsAct`. Цикл либо несколько вызовов
одного helper для того же корабля блокируются как
`runtime-item-list-mutated-during-star-act`. В текущем ходе безопаснее только
пометить предметы/корабль, завершить обработчик и удалить их после отдельной
границы хода.

### Приказы, гиперпространство и временные боевые цели

`ShipStar(ship)` нельзя использовать как первый способ выяснить, где находится
мобильный корабль. Для посаженного корабля вызов подтверждённо способен дать
`EAccessViolation`; во время переходного состояния после `OrderTakeOff`
`ShipInNormalSpace` может стать истинным раньше полной готовности внутренних
ссылок. `runtime-shipstar-on-docked-ship` требует до вызова:

1. отдельно обработать `GetShipPlanet(ship)`/`GetShipRuins(ship)`;
2. завершить текущий ход после `OrderTakeOff`;
3. доказать `!ShipIsTakeoff(ship)` и `ShipInNormalSpace(ship)`.

`OrderNone`, `OrderFollowShip`, `OrderJump`, `OrderLanding`, setter-форма
`OrderLock` и `ShipSetBad` могут отменить незавершённый переход. Если функция
сначала меняет приказ, а ниже проверяет `ShipInHyperSpace` для того же корабля,
`runtime-order-rewrite-in-hyperspace` блокирует поздний guard. Проверка должна
доминировать над первым изменением:

```text
if(ShipInHyperSpace(ship) || ShipInHyperSpace(leader)) exit;
ShipSetBad(ship, 0);
OrderNone(ship);
```

Цель из `ShipGetBad` является временным raw handle: после разрыва систем она
может ссылаться на уже удалённый объект. До сопоставления по `==` с кораблём,
заново полученным из живого списка текущей звезды, запрещены `Ship*`, `Order*`,
`Coord*`, `GetShipPlanet`, `GetShipRuins`, `Id` и `Name`; это блокирующая ошибка
`runtime-shipgetbad-opaque-dereference`. Даже после разрешения перед
`OrderFollowShip` нужны normal-space обоих кораблей и одинаковая `ShipStar`.
Сырое распространение через `GroupSetBad` дополнительно даёт intent-sensitive
`runtime-stale-shipgetbad-follow`.

`StarShips(star, index)` перечисляет и мобильные корабли, и станции. Поэтому
равенство свежего элемента с raw handle ещё не разрешает `ShipIsTakeoff`:
сначала должна доминировать проверка `ShipTypeN(fresh_ship) < t_RC`. Без неё
сборка блокируется как
`runtime-shipistakeoff-on-unproven-starships-member`.

Глобальный Turn-код может войти несколько раз за одну игровую дату. Cleanup,
который удаляет один корабль и делает только `exit`, получает предупреждение
`runtime-cleanup-without-turn-gate`. Для гарантии «один объект за ход» используйте
парный барьер `if(CurTurn() < next_cleanup_turn) exit;` и назначайте
`next_cleanup_turn = CurTurn() + 1`. Предупреждение становится блокирующим в
`--strict`/`--warnings-as-errors`.

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
кода и типов. Небольшой проект получает 60 секунд без подтверждённого прогресса
и не менее 600 секунд общего времени. Для крупных проектов оба окна растут по
размеру, числу объектов и строк кода. Ожидаемый файл, файловый ввод-вывод RScript
и переход шага скрытой автоматизации сдвигают окно; одна лишь загрузка CPU его
не сдвигает. Значение `0` у обоих параметров полностью отключает ограничения.

Каждое `runtime_issues` в таком отчёте помечено
`analysis_origin: decompiled-rson`. Для правил, доказательство которых может
измениться при канонизации графа (сейчас это
`runtime-turn-direct-world-access`), добавляется
`canonicalization_sensitive: true`. Флаг не подавляет предупреждение: сначала
сверьте его с авторским SOURCE RSON, если исходник доступен.

Если скрипт не создаёт языковых строк, RScript записывает
`DATA/Script/Lang.dat` как два байта `FF FE` — пустой UTF-16LE с BOM. Это
корректный специальный формат, а не повреждённый BlockPar. Команда
`dat validate` распознаёт его без запуска BlockParEditor; исключение действует
только для точного пути `DATA/Script/Lang.dat` и точного содержимого `FF FE`.
Если такой файл явно передан в `script decompile --lang-dat`, ModKit не включает
зависимый от GUI импорт диалогов и записывает фазу `import-dialogs: skipped` с
причиной `empty-rscript-lang-dat`.

Если непустой Lang.dat вызывает внутреннюю ошибку RScript `TFileEC`, ModKit
показывает путь временного файла, его доступность и возможную кодировку в
структурированном поле `lang_import.diagnostic`. По умолчанию операция
прерывается: молча терять диалоги нельзя. Только после осознанного решения можно
повторить проверенное восстановление без языка:

```powershell
python srhd.py script decompile D:\work\Mod.scr D:\work\Mod.rson `
  --lang-dat D:\work\Lang.dat --fallback-without-lang --json
```

В успешном результате при этом остаются `dialogs_imported: false`,
`lang_import.status: failed-fallback` и исходная диагностика ошибки.

## Ограничения графа RScript 4.10f

- В одном проекте допускается не более четырёх объектов `TGroup`. Пятый объект
  способен повесить компилятор вместо обычного сообщения; `script validate`
  блокирует это как `rscript-tgroup-hard-limit` и перечисляет объекты.
- Номера `DMsg.Num` и `AMsg.Num` глобальны внутри своей категории, должны быть
  неотрицательными и уникальными. Разреженная нумерация пока выдаёт warning,
  потому что существующие проекты могут использовать её намеренно.
- Имена `TDialog` должны быть уникальны. Третий аргумент `InjectAnswer` — это
  `GAnswerData`, а не номер ответа или другой ветви.
- `TDialogAnswer.Msg` является сообщением. Произвольный `InjectAnswer`,
  `DChange`, `DAdd` или нестандартное RScript-выражение внутри `Msg` считается
  ошибкой. Каноническая строка декомпилятора вида
  `DAnswer(CT("Script.Mod_Name.41"))` поддерживается.
- Подтверждённо опасен forward-reference: диалоговый handler не должен работать
  с persistent-массивом `TVar`, чей объект расположен позднее handler в графе.
  Переместите массив до обработчика либо измените схему проекта. Это отдельное
  ограничение компилятора, а не запрет массивов во всех диалогах.

## Guard объектов и persistent-схема

Не полагайтесь на короткое замыкание `&&` и `||` вокруг вызовов, принимающих
игровой объект. RScript может вычислить опасную часть выражения и обратиться к
нулевой или устаревшей ссылке. Guard должен завершиться раньше вызова:

```text
// Небезопасно
if(ship && ShipInNormalSpace(ship) && ShipStar(ship)==target) ...

// Безопасная форма
if(!ship) return;
if(!ShipInNormalSpace(ship)) return;
star=ShipStar(ship);
```

Тот же принцип обязателен для результатов `GalaxyStar` и `StarRuins`.
Ограниченный индекс цикла не доказывает, что движок вернул живой handle, а
строковый аргумент `StarRuins` уже выполняет типизированный поиск:

```text
// Небезопасно: star не проверен, а base повторно разыменовывается в том же &&
dword star = GalaxyStar(i);
dword base = StarRuins(star, 'PB');
if(base && ShipTypeN(base) == t_PB) ...

// Безопасно
dword star = GalaxyStar(i);
if(!star) continue;
dword base = StarRuins(star, 'PB');
if(!base) continue;
// base уже найден как 'PB'; повторный ShipTypeN не требуется
```

`runtime-object-api-without-explicit-guard` отслеживает такие присваивания и
алиасы между строками. Обычное тело `if(base) Id(base);` распознаётся как
отдельно защищённое; опасным остаётся вызов справа от `base && ...` или
`!base || ...`.

Присваивание голому имени, которого нет среди `TVar`, параметров, локальных
переменных или подтверждённого API, блокируется как
`runtime-code-uses-unregistered-tvar`. Объявление `var a,b,c;` распознаётся как
три локальных имени; повтор одного имени в соседней ветви того же handler всё
равно запрещён компилятором.

Новый persistent-массив нельзя инициализировать только под старым общим флагом
первого запуска: загруженное сохранение уже могло пройти этот флаг. Используйте
отдельную версию хранилища и миграцию до первого `Array*`. Команда
`script compare-scr` и Python API `compare_storage_schemas(old, new)` показывают
массивы, скрытые под legacy gate. После миграции завершайте текущий handler,
чтобы код ниже не продолжил работу с прежним состоянием.

## Диалоги, Ether и купленные воины

`DChange` и `DAdd` должны ссылаться на существующие глобальные номера.
Именованные цели `InjectAnswer`/`AddDialogInject` должны существовать и иметь
достижимый обработчик. Self-target допустим для динамического меню и поэтому
помечается только как `info`. `fastexit` в ветви, инъецированной на
планету/станцию, требует отдельной проверки поведения в игре.

Если `AddDialogInject` целиком зависит от persistent-флага, который
устанавливается только более поздним `Turn`, ModKit выдаёт информационное
`runtime-dialog-inject-delayed-persistent-gate`. Это не ошибка для намеренно
отложенного сюжетного диалога, но панель управления или отладки лучше отделять
от поздней инициализации мира.

Литеральный Ether type `8` подтверждён как сообщение `mp_ShipMinus` с иконкой
ключа. Повторная публикация того же постоянного ID после `EtherDelete` в одном
handler может затереть состояние другого сообщения и отмечается warning.

После `BuyWarrior` корабль наследует игровые значения по умолчанию. Если его
нужно отпустить через `ShipOut` или `ShipFreeFlight`, заранее задайте дом через
`ShipStatistic(ship,10,home)`. ModKit выдаёт по этому шаблону информационное
сообщение, поскольку намерение автора статически доказать нельзя.

## Сборка

```powershell
cd D:\SRHD_Modding\Tools\SRHDModKit
python srhd.py script validate D:\work\MyScript.rson
python srhd.py script build D:\work\MyScript.rson `
  --scr D:\work\MyScript.scr --lang D:\work\MyScript.txt
```

`script build --timeout 0` отключает оба дедлайна компилятора. Без параметра
малый проект получает 60-секундное скользящее окно без прогресса и общий предел
от 600 секунд; крупный получает адаптивно больше времени. Положительное значение
`--timeout` задаёт явный общий предел и ограничивает окно без прогресса.

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
