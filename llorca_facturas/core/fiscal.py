"""
Validaciones fiscales y bancarias con su algoritmo oficial (no heurísticas):
- NIF de persona física (DNI), NIE y CIF/NIF de persona jurídica (dígito de control).
- IBAN (ISO 13616, módulo 97) y, para cuentas españolas, los dos dígitos de control del CCC.
"""
from __future__ import annotations

import re

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_CIF_LETTERS = "JABCDEFGHI"
_CIF_ORG = "ABCDEFGHJNPQRSUVW"


def normalize_nif(value: str | None) -> str:
    if not value:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    if s.startswith("ES") and len(s) == 11:  # NIF-IVA intracomunitario ESB12345678
        s = s[2:]
    return s


def nif_type(nif: str) -> str:
    n = normalize_nif(nif)
    if re.fullmatch(r"\d{8}[A-Z]", n):
        return "DNI"
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", n):
        return "NIE"
    if re.fullmatch(r"[KLM]\d{7}[A-Z]", n):
        return "NIF especial"
    if re.fullmatch(rf"[{_CIF_ORG}]\d{{7}}[0-9A-J]", n):
        return "CIF"
    return "desconocido"


def validate_nif(value: str | None) -> tuple[bool, str]:
    """Devuelve (es_valido, explicación)."""
    n = normalize_nif(value)
    if not n:
        return False, "NIF vacío"
    t = nif_type(n)
    if t == "DNI":
        ok = _DNI_LETTERS[int(n[:8]) % 23] == n[8]
        return ok, "DNI válido" if ok else f"Letra de control incorrecta (debería ser {_DNI_LETTERS[int(n[:8]) % 23]})"
    if t == "NIE":
        num = "XYZ".index(n[0]).__str__() + n[1:8]
        ok = _DNI_LETTERS[int(num) % 23] == n[8]
        return ok, "NIE válido" if ok else "Letra de control del NIE incorrecta"
    if t == "NIF especial":
        num = n[1:8]
        ok = _DNI_LETTERS[int(num) % 23] == n[8]
        return ok, "NIF especial válido" if ok else "Letra de control incorrecta"
    if t == "CIF":
        letter, digits, control = n[0], n[1:8], n[8]
        even = sum(int(digits[i]) for i in (1, 3, 5))
        odd = 0
        for i in (0, 2, 4, 6):
            d = int(digits[i]) * 2
            odd += d // 10 + d % 10
        c = (10 - (even + odd) % 10) % 10
        exp_digit, exp_letter = str(c), _CIF_LETTERS[c]
        if letter in "PQRSNW":
            ok = control == exp_letter
        elif letter in "ABEH":
            ok = control == exp_digit
        else:
            ok = control in (exp_digit, exp_letter)
        return ok, "CIF válido" if ok else f"Dígito de control incorrecto (esperado {exp_digit}/{exp_letter})"
    return False, "Formato de NIF/CIF no reconocido"


def normalize_iban(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()


def validate_iban(value: str | None) -> tuple[bool, str]:
    iban = normalize_iban(value)
    if not iban:
        return False, "IBAN vacío"
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False, "Formato IBAN no válido"
    if iban.startswith("ES") and len(iban) != 24:
        return False, f"Un IBAN español tiene 24 caracteres (tiene {len(iban)})"
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    if int(numeric) % 97 != 1:
        return False, "Dígitos de control IBAN incorrectos (módulo 97)"
    if iban.startswith("ES"):
        ok, msg = _validate_ccc(iban[4:])
        if not ok:
            return False, msg
    return True, "IBAN válido"


def _validate_ccc(ccc: str) -> tuple[bool, str]:
    """Dígitos de control internos de la cuenta española (CCC de 20 dígitos)."""
    if not re.fullmatch(r"\d{20}", ccc):
        return False, "CCC no numérico"
    weights = [1, 2, 4, 8, 5, 10, 9, 7, 3, 6]

    def dc(digits: str) -> int:
        s = sum(int(d) * w for d, w in zip(digits.zfill(10), weights))
        r = 11 - s % 11
        return {10: 1, 11: 0}.get(r, r)

    d1 = dc("00" + ccc[:8])
    d2 = dc(ccc[10:])
    if f"{d1}{d2}" != ccc[8:10]:
        return False, "Dígitos de control de la cuenta (CCC) incorrectos"
    return True, "CCC válido"


def format_iban(value: str | None) -> str:
    iban = normalize_iban(value)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def mask_iban(value: str | None) -> str:
    iban = normalize_iban(value)
    if len(iban) < 8:
        return iban
    return f"{iban[:4]} **** **** {iban[-4:]}"
