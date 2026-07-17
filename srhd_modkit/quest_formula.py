from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


class QuestFormulaError(ValueError):
    """A TGE formula cannot be tokenized or parsed safely."""


@dataclass(frozen=True, slots=True)
class FormulaToken:
    kind: str
    text: str
    position: int


@dataclass(frozen=True, slots=True)
class FormulaNode:
    kind: str
    value: float | int | None = None
    operator: str | None = None
    children: tuple["FormulaNode", ...] = ()

    @property
    def parameter_indices(self) -> frozenset[int]:
        values: set[int] = set()
        if self.kind == "parameter" and isinstance(self.value, int):
            values.add(self.value)
        for child in self.children:
            values.update(child.parameter_indices)
        return frozenset(values)


_KEYWORDS = {"mod", "div", "to", "in", "and", "or"}
_TWO_CHAR = {"..", "<>", ">=", "<="}
_ONE_CHAR = set("()[]/*+-=;<>")


def _tokens(value: str) -> Iterator[FormulaToken]:
    value = value.replace("\r", " ").replace("\n", " ")
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            index += 1
            continue
        pair = value[index : index + 2]
        if pair in _TWO_CHAR:
            yield FormulaToken(pair, pair, index)
            index += 2
            continue
        if char in _ONE_CHAR:
            yield FormulaToken(char, char, index)
            index += 1
            continue
        if char.isdigit():
            start = index
            dot_seen = False
            trailing_space: int | None = None
            while index < len(value):
                current = value[index]
                if current.isdigit():
                    trailing_space = None
                elif current in ".,":
                    if dot_seen or value[index : index + 2] in {"..", ",,"}:
                        break
                    dot_seen = True
                    trailing_space = None
                elif current == " ":
                    if trailing_space is None:
                        trailing_space = index
                else:
                    break
                index += 1
            if trailing_space is not None:
                index = trailing_space
            text = value[start:index]
            yield FormulaToken("number", text, start)
            continue
        if char.isascii() and (char.isalpha() or char == "_"):
            start = index
            while index < len(value) and value[index].isascii() and (
                value[index].isalnum() or value[index] == "_"
            ):
                index += 1
                text = value[start:index]
                # TGE accepts operators glued to the following number, e.g. mod1000.
                if text in _KEYWORDS:
                    break
            text = value[start:index]
            yield FormulaToken(text if text in _KEYWORDS else "identifier", text, start)
            continue
        raise QuestFormulaError(f"Неизвестный символ {char!r} в позиции {index}")
    yield FormulaToken("end", "", len(value))


_PRECEDENCE: tuple[frozenset[str], ...] = (
    frozenset(),
    frozenset({"div", "mod"}),
    frozenset({"*", "/"}),
    frozenset({"+", "-"}),
    frozenset({"to"}),
    frozenset(),
    frozenset({">=", "<=", ">", "<", "=", "<>", "in"}),
    frozenset({"and"}),
    frozenset({"or"}),
)


class _FormulaParser:
    def __init__(self, value: str):
        self._tokens = tuple(_tokens(value))
        self._index = 0

    @property
    def current(self) -> FormulaToken:
        return self._tokens[self._index]

    def advance(self) -> FormulaToken:
        token = self.current
        if token.kind != "end":
            self._index += 1
        return token

    def parse(self) -> FormulaNode:
        node = self.expression(8)
        if self.current.kind != "end":
            raise QuestFormulaError(
                f"Лишние данные {self.current.text!r} в позиции {self.current.position}"
            )
        return node

    def expression(self, precedence: int) -> FormulaNode:
        if precedence == 0:
            return self.primary()
        left = self.expression(precedence - 1)
        while self.current.kind in _PRECEDENCE[precedence]:
            operator = self.advance().kind
            right = self.expression(precedence - 1)
            left = FormulaNode("binary", operator=operator, children=(left, right))
        return left

    def primary(self) -> FormulaNode:
        token = self.current
        if token.kind == "number":
            self.advance()
            normalized = token.text.replace(" ", "").replace(",", ".")
            try:
                return FormulaNode("number", value=float(normalized))
            except ValueError as exc:
                raise QuestFormulaError(
                    f"Некорректное число {token.text!r} в позиции {token.position}"
                ) from exc
        if token.kind == "(":
            self.advance()
            node = self.expression(8)
            if self.current.kind != ")":
                raise QuestFormulaError(
                    f"Ожидалась ')' в позиции {self.current.position}"
                )
            self.advance()
            return node
        if token.kind == "[":
            return self.bracket()
        if token.kind == "-":
            self.advance()
            return FormulaNode("unary", operator="-", children=(self.primary(),))
        if token.kind == "end":
            raise QuestFormulaError(f"Ожидалось значение в позиции {token.position}")
        raise QuestFormulaError(
            f"Ожидалось значение, получено {token.text!r} в позиции {token.position}"
        )

    def bracket(self) -> FormulaNode:
        self.advance()
        token = self.current
        if token.kind == "identifier" and token.text.startswith("p") and token.text[1:].isdigit():
            parameter = int(token.text[1:]) - 1
            if parameter < 0:
                raise QuestFormulaError(
                    f"Номер параметра должен начинаться с 1 в позиции {token.position}"
                )
            self.advance()
            if self.current.kind != "]":
                raise QuestFormulaError(
                    f"Ожидалась ']' после {token.text!r} в позиции {self.current.position}"
                )
            self.advance()
            return FormulaNode("parameter", value=parameter)

        ranges: list[FormulaNode] = []
        while self.current.kind != "]":
            if self.current.kind == "end":
                raise QuestFormulaError("Диапазон не закрыт символом ']'")
            if self.current.kind == ";":
                self.advance()
                continue
            start = self.expression(8)
            if self.current.kind == "..":
                self.advance()
                end = self.expression(8)
                ranges.append(FormulaNode("range-part", children=(start, end)))
            elif self.current.kind in {";", "]"}:
                ranges.append(FormulaNode("range-part", children=(start,)))
            else:
                raise QuestFormulaError(
                    f"Неожиданный токен {self.current.text!r} внутри диапазона "
                    f"в позиции {self.current.position}"
                )
        self.advance()
        if not ranges:
            raise QuestFormulaError("Пустой диапазон [] не допускается")
        return FormulaNode("range", children=tuple(ranges))


def parse_quest_formula(value: str) -> FormulaNode:
    """Parse the formula syntax used by TGE/QM/QMM without executing it."""

    if not value.strip():
        raise QuestFormulaError("Формула пуста")
    return _FormulaParser(value).parse()


def validate_quest_formula(value: str, *, params_count: int | None = None) -> FormulaNode:
    node = parse_quest_formula(value)
    if params_count is not None:
        invalid = sorted(index + 1 for index in node.parameter_indices if index >= params_count)
        if invalid:
            raise QuestFormulaError(
                "Формула ссылается на отсутствующие параметры: "
                + ", ".join(f"p{value}" for value in invalid)
            )
    return node
