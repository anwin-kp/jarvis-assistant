"""Exact answers the AI model is bad at doing in its head: arithmetic, unit and currency conversion, date math,
word definitions. Each is one deterministic call (no step-by-step guessing, no made-up numbers)."""

import ast
import math
import operator
import re
from datetime import date, timedelta

import requests

from jarvis.tools.registry import ToolRegistry

UA = {"User-Agent": "jarvis-personal-assistant/0.1"}

# ------------------------------------------------------------------ calculate

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "sin": lambda x: math.sin(math.radians(x)), "cos": lambda x: math.cos(math.radians(x)),
          "tan": lambda x: math.tan(math.radians(x)), "log": math.log10, "log10": math.log10, "ln": math.log,
          "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
          "exp": math.exp}
_CONSTS = {"pi": math.pi, "e": math.e}
_WORDS = [(r"\bdivided by\b", "/"), (r"\bover\b", "/"), (r"\b(?:times|multiplied by|into)\b", "*"),
          (r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\bmod(?:ulo)?\b", "%"), (r"\bsquared\b", "**2"),
          (r"\bcubed\b", "**3"), (r"\bto the power of\b", "**"), (r"\bsquare root of\b", "sqrt"),
          (r"[×x](?=\s*[\d(.])", "*"), (r"÷", "/"), (r"\^", "**"), (r"(?<=\d),(?=\d{3}\b)", "")]


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 1000:
            raise ValueError("that exponent is too large")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and (args[0] > 1000 or args[0] != int(args[0])):
            raise ValueError("factorial needs a whole number up to 1000")
        return _FUNCS[node.func.id](*args)
    raise ValueError("only numbers, + - * / % ** ( ) and sqrt/sin/cos/tan/log/ln/abs/round are allowed")


def calculate(expression: str):
    """'15% of 2400', '12 times 7', '(3+4)^2', 'sqrt(2)' -> exact result. Safe: no Python is executed."""
    e = expression.lower().strip().rstrip("=?. ")
    e = re.sub(r"^(?:what(?:'s| is)|calculate|compute|how much is)\s+", "", e)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", e)  # 15% of 2400
    e = re.sub(r"(\d+(?:\.\d+)?)\s*percent\s*of\s*", r"(\1/100)*", e)
    for pattern, repl in _WORDS:
        e = re.sub(pattern, repl, e)
    e = re.sub(r"sqrt\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", e)
    if len(e) > 200:
        raise ValueError("that expression is too long")
    value = _eval(ast.parse(e, mode="eval"))
    if isinstance(value, float):
        if math.isinf(value) or math.isnan(value):
            raise ValueError("the result isn't a finite number")
        value = int(value) if value.is_integer() and abs(value) < 1e15 else round(value, 10)
    return value, e


# ------------------------------------------------------------------ units

_UNITS: dict[str, tuple[str, float]] = {}  # alias -> (dimension, factor to the base unit)


def _add(dim: str, factor: float, *aliases: str) -> None:
    for a in aliases:
        _UNITS[a] = (dim, factor)


for _dim, _rows in {
    "length (m)": [(1, "m", "meter", "meters", "metre", "metres"), (1000, "km", "kilometer", "kilometers",
                   "kilometre", "kilometres"), (0.01, "cm", "centimeter", "centimeters", "centimetre", "centimetres"),
                   (0.001, "mm", "millimeter", "millimeters", "millimetre", "millimetres"),
                   (1609.344, "mi", "mile", "miles"), (0.9144, "yd", "yard", "yards"),
                   (0.3048, "ft", "foot", "feet"), (0.0254, "in", "inch", "inches"),
                   (1852, "nmi", "nautical mile", "nautical miles")],
    "mass (kg)": [(1, "kg", "kilo", "kilos", "kilogram", "kilograms"), (0.001, "g", "gram", "grams"),
                  (1e-6, "mg", "milligram", "milligrams"), (0.45359237, "lb", "lbs", "pound", "pounds"),
                  (0.028349523125, "oz", "ounce", "ounces"), (1000, "t", "tonne", "tonnes", "ton", "tons"),
                  (6.35029318, "st", "stone", "stones")],
    "volume (l)": [(1, "l", "liter", "liters", "litre", "litres"), (0.001, "ml", "milliliter", "milliliters",
                   "millilitre", "millilitres"), (3.785411784, "gal", "gallon", "gallons"),
                   (0.2365882365, "cup", "cups"), (0.01478676478125, "tbsp", "tablespoon", "tablespoons"),
                   (0.00492892159375, "tsp", "teaspoon", "teaspoons"), (0.0295735295625, "fl oz", "floz",
                   "fluid ounce", "fluid ounces"), (1000, "m3", "cubic meter", "cubic meters")],
    "speed (m/s)": [(1, "m/s", "mps", "meters per second"), (1 / 3.6, "km/h", "kmh", "kph", "kmph",
                    "kilometers per hour", "kilometres per hour"), (0.44704, "mph", "miles per hour"),
                    (0.514444, "knot", "knots", "kn")],
    "area (m2)": [(1, "m2", "sq m", "square meter", "square meters", "square metre", "square metres"),
                  (1e6, "km2", "sq km", "square kilometer", "square kilometers"),
                  (0.09290304, "ft2", "sq ft", "sqft", "square foot", "square feet"),
                  (4046.8564224, "acre", "acres"), (10000, "ha", "hectare", "hectares"),
                  (0.83612736, "sq yd", "square yard", "square yards")],
    "data (bytes)": [(1, "b", "byte", "bytes"), (1024, "kb", "kilobyte", "kilobytes"),
                     (1024**2, "mb", "megabyte", "megabytes"), (1024**3, "gb", "gigabyte", "gigabytes"),
                     (1024**4, "tb", "terabyte", "terabytes")],
    "time (s)": [(1, "s", "sec", "second", "seconds"), (60, "min", "minute", "minutes"),
                 (3600, "h", "hr", "hour", "hours"), (86400, "day", "days"), (604800, "week", "weeks"),
                 (31557600, "year", "years")],
}.items():
    for _f, *_aliases in _rows:
        _add(_dim, _f, *_aliases)
_TEMPS = {"c": "c", "celsius": "c", "°c": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "°f": "f",
          "k": "k", "kelvin": "k"}


def _unit(u: str) -> str:
    return re.sub(r"\s+", " ", u.lower().strip().removeprefix("degrees ").removeprefix("degree ")).strip(" .")


def convert_units(value: float, from_unit: str, to_unit: str) -> float:
    a, b = _unit(from_unit), _unit(to_unit)
    if a in _TEMPS and b in _TEMPS:
        c = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[_TEMPS[a]]
        return {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[_TEMPS[b]]
    if a not in _UNITS or b not in _UNITS:
        raise ValueError(f"I don't know the unit '{from_unit if a not in _UNITS else to_unit}'")
    (da, fa), (db, fb) = _UNITS[a], _UNITS[b]
    if da != db:
        raise ValueError(f"can't convert {da.split()[0]} to {db.split()[0]}")
    return value * fa / fb


def _nice(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.6g}" if abs(x) >= 1e-4 else f"{x:.4g}"


# ------------------------------------------------------------------ currency

CURRENCY_WORDS = {"dollar": "USD", "dollars": "USD", "usd": "USD", "$": "USD", "rupee": "INR", "rupees": "INR",
                  "rs": "INR", "inr": "INR", "₹": "INR", "euro": "EUR", "euros": "EUR", "€": "EUR",
                  "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "£": "GBP", "yen": "JPY", "¥": "JPY",
                  "dirham": "AED", "dirhams": "AED", "riyal": "SAR", "riyals": "SAR", "yuan": "CNY",
                  "renminbi": "CNY", "aussie dollar": "AUD", "canadian dollar": "CAD", "singapore dollar": "SGD"}


def currency_code(name: str) -> str:
    n = name.lower().strip()
    return CURRENCY_WORDS.get(n) or n.upper()


# ------------------------------------------------------------------ dates


def parse_date(text: str, today: date | None = None) -> date:
    t = (text or "").lower().strip()
    today = today or date.today()
    if t in ("", "today", "now"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(t[:10])
    except ValueError as e:
        raise ValueError(f"give the date as YYYY-MM-DD (got '{text}')") from e


def date_math(target: str = "", days: int = 0, from_date: str = "", today: date | None = None) -> str:
    start = parse_date(from_date, today)
    if target:
        end = parse_date(target, today)
        n = (end - start).days
        weeks, rest = divmod(abs(n), 7)
        span = f"{abs(n)} day{'s' * (abs(n) != 1)}" + (f" ({weeks} weeks and {rest} days)" if weeks else "")
        when = f"{end:%A %d %B %Y}"
        if n == 0:
            return f"That's today ({when})."
        return f"{when} is {span} {'from' if n > 0 else 'before'} {start:%d %B %Y}."
    end = start + timedelta(days=int(days))
    return f"{int(days)} days {'after' if days >= 0 else 'before'} {start:%d %B %Y} is {end:%A %d %B %Y}."


# ------------------------------------------------------------------ registration


def register(reg: ToolRegistry) -> None:
    @reg.tool("calculate", "Exact arithmetic. Use for ANY maths instead of working it out yourself: '15% of 2400', "
              "'12 times 7', '(3+4)^2', 'sqrt(2)', 'sin(30)' (degrees). Not for pressing buttons in the Calculator "
              "app (that's operate_app).",
              {"expression": {"type": "string"}}, group="utility", read_only=True)
    def calculate_tool(expression: str):
        try:
            value, expr = calculate(expression)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as e:
            msg = "division by zero" if isinstance(e, ZeroDivisionError) else str(e) or "not a valid expression"
            return f"ERROR: couldn't calculate '{expression}': {msg}."
        shown = f"{value:,}" if isinstance(value, int) else f"{value:,.10g}"
        return f"{expression.strip()} = {shown}" + (f"  (computed as {expr})" if expr != expression.strip() else "")

    @reg.tool("convert_units", "Convert a value between units: length, weight, volume, speed, area, data size, "
              "time and temperature (e.g. 5 miles -> km, 70 F -> C, 2 cups -> ml).",
              {"value": {"type": "number"}, "from_unit": {"type": "string"}, "to_unit": {"type": "string"}},
              group="utility", read_only=True)
    def convert_units_tool(value: float, from_unit: str, to_unit: str):
        try:
            out = convert_units(float(value), from_unit, to_unit)
        except ValueError as e:
            return f"ERROR: {e}."
        return f"{_nice(float(value))} {from_unit} = {_nice(out)} {to_unit}"

    @reg.tool("convert_currency", "Convert money between currencies with today's exchange rates (ECB reference "
              "rates), e.g. 100 USD -> INR. Codes or names: USD, INR, EUR, GBP, dollars, rupees...",
              {"amount": {"type": "number"}, "from_currency": {"type": "string"}, "to_currency": {"type": "string"}},
              group="utility", read_only=True, remember_last="disk")
    def convert_currency(amount: float, from_currency: str, to_currency: str):
        a, b = currency_code(from_currency), currency_code(to_currency)
        if a == b:
            return f"{_nice(float(amount))} {a} = {_nice(float(amount))} {b}"
        r = requests.get("https://api.frankfurter.app/latest", headers=UA, timeout=8,
                         params={"amount": float(amount), "from": a, "to": b})
        if r.status_code == 404 or (r.ok and b not in (r.json().get("rates") or {})):
            return f"ERROR: I don't have exchange rates for {a} to {b}."
        r.raise_for_status()
        j = r.json()
        return f"{_nice(float(amount))} {a} = {j['rates'][b]:,.2f} {b} (exchange rate of {j.get('date', 'today')})"

    @reg.tool("date_math", "Exact date arithmetic. target: how many days until/since a date (YYYY-MM-DD, today, "
              "tomorrow). days: the date that many days after (negative = before) from_date (default today).",
              {"target": {"type": "string"}, "days": {"type": "integer"}, "from_date": {"type": "string"}},
              required=[], group="utility", read_only=True)
    def date_math_tool(target: str = "", days: int = 0, from_date: str = ""):
        try:
            return date_math(target, int(days or 0), from_date)
        except (ValueError, OverflowError) as e:
            return f"ERROR: {e}."

    @reg.tool("define_word", "Dictionary definition of an English word (meanings, part of speech, example).",
              {"word": {"type": "string"}}, group="utility", read_only=True)
    def define_word(word: str):
        w = word.strip().lower()
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{requests.utils.quote(w)}",
                         headers=UA, timeout=8)
        if r.status_code == 404:
            return f"ERROR: '{word}' isn't in the dictionary."
        r.raise_for_status()
        lines = []
        entry = r.json()[0]
        if ph := entry.get("phonetic"):
            lines.append(f"{entry.get('word', w)} {ph}")
        for m in entry.get("meanings", [])[:3]:
            for d in m.get("definitions", [])[:2]:
                ex = f' (e.g. "{d["example"]}")' if d.get("example") else ""
                lines.append(f"- {m.get('partOfSpeech', '')}: {d.get('definition', '')}{ex}")
        return "\n".join(lines) or f"No definition found for '{word}'."
