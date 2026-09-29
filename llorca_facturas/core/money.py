"""
Aritmética monetaria exacta.

Regla de oro del proyecto: NINGÚN importe pasa por float.
- Internamente se trabaja con decimal.Decimal.
- En base de datos se guarda en CÉNTIMOS como INTEGER (sin errores de redondeo).
- El redondeo es ROUND_HALF_UP a 2 decimales (criterio habitual en facturación
  española y el que aplica la AEAT para la cuota de IVA por tipo impositivo).
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, getcontext

getcontext().prec = 28

CENT = Decimal("0.01")
ZERO = Decimal("0")


def D(value) -> Decimal:
    """Convierte cualquier cosa razonable a Decimal SIN pasar por float."""
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Los float solo pueden venir de librerías externas: se convierten vía repr
        return Decimal(str(float(value)))   # float/np.float64 -> texto corto, nunca repr (numpy 2 lo decora)
    s = str(value).strip()
    if _MACHINE.match(s):
        # Formato máquina (el que devuelve la IA y la BD): punto decimal, sin miles
        return Decimal(s)
    return parse_amount(s)


def q2(value) -> Decimal:
    """Redondea a céntimos (ROUND_HALF_UP)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def to_cents(value) -> int:
    return int((q2(value) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(cents) -> Decimal:
    if cents is None:
        return ZERO
    return (Decimal(int(cents)) / 100).quantize(CENT)


_CLEAN = re.compile(r"[^\d,.\-]")
_MACHINE = re.compile(r"^-?\d+(\.\d+)?$")


def parse_amount(text: str) -> Decimal:
    """
    Interpreta importes en formato español o anglosajón:
      '1.234,56 €'  -> 1234.56      '2,742.75 EUR' -> 2742.75
      '1351,2'      -> 1351.2       '-1.850,99€'   -> -1850.99
      '(1.000,00)'  -> -1000.00     '15.000'       -> 15000 (miles)
      '−594,00'     -> -594.00 (signo menos unicode)
    """
    if text is None:
        return ZERO
    s = str(text).strip()
    if not s:
        return ZERO
    neg = False
    s = s.replace("\u2212", "-").replace("\u200b", "").replace("\xa0", " ")
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = _CLEAN.sub("", s)
    if s.count("-"):
        neg = neg or s.strip().startswith("-") or s.strip().endswith("-")
        s = s.replace("-", "")
    if not s or not re.search(r"\d", s):
        return ZERO

    last_comma, last_dot = s.rfind(","), s.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        # El separador que aparece el último es el decimal
        if last_comma > last_dot:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif last_comma >= 0:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            s = s.replace(",", ".")               # 1351,2 / 12,50
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] != "0":
            # Ambiguo '1,234': en facturas españolas lo más habitual es decimal con 3 cifras
            # (precios unitarios). Se interpreta como decimal.
            s = s.replace(",", ".")
        elif len(parts) > 2:
            s = s.replace(",", "")                # 1,234,567
        else:
            s = s.replace(",", ".")
    elif last_dot >= 0:
        parts = s.split(".")
        if len(parts) > 2:
            s = s.replace(".", "")                # 1.234.567
        elif len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] not in ("0", ""):
            s = s.replace(".", "")                # 15.000 -> miles (formato español)
    try:
        val = Decimal(s)
    except InvalidOperation:
        return ZERO
    return -val if neg else val


def fmt_eur(value, decimals: int = 2, sign: bool = False) -> str:
    """Formato español: 1.234.567,89 €"""
    v = D(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    s = f"{abs(v):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    prefix = "-" if v < 0 else ("+" if sign and v > 0 else "")
    return f"{prefix}{s} €"


def fmt_num(value, decimals: int = 2) -> str:
    v = D(value).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    s = f"{abs(v):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + s


def pct(part, whole) -> Decimal:
    w = D(whole)
    if w == 0:
        return ZERO
    return (D(part) / w * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def amount_variants(value) -> set[str]:
    """
    Representaciones textuales habituales de un importe, para comprobar que un
    importe extraído por la IA aparece LITERALMENTE en el PDF (anclaje).
    """
    v = q2(value)
    a = abs(v)
    ent, dec = f"{a:.2f}".split(".")
    ent_int = int(ent)
    es_miles = f"{ent_int:,}".replace(",", ".")
    en_miles = f"{ent_int:,}"
    out = {
        f"{es_miles},{dec}", f"{ent},{dec}", f"{en_miles}.{dec}", f"{ent}.{dec}",
    }
    if dec.endswith("0"):
        out |= {f"{es_miles},{dec[0]}", f"{ent},{dec[0]}", f"{ent}.{dec[0]}"}
    if dec == "00":
        out |= {es_miles, ent, en_miles}
    return out
