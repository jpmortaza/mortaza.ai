"""Parser de expressões cron.

Tenta usar `croniter` (mais completo) e cai num parser mínimo se não tiver.
O fallback suporta os padrões mais comuns que o usuário do mtzcode vai usar:

    "*/N * * * *"      a cada N minutos
    "0 H * * *"        todo dia às H:00
    "0 H * * 1-5"      seg-sex às H:00
    "M H * * *"        diariamente em M minutos da hora H
    "M H * * D"        em dias da semana específicos (0-6, 0=domingo)

Não pretende ser conformante 100% com cron padrão — só o suficiente
pra agendar prompts de assistente de código.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Tenta usar croniter (preferencial). Marca a flag pra escolher implementação.
try:
    from croniter import croniter as _croniter  # type: ignore

    _HAS_CRONITER = True
except Exception:  # ImportError ou qualquer falha de import lateral
    _HAS_CRONITER = False


# ----------------------------------------------------------------------
# API pública
# ----------------------------------------------------------------------
def next_run(cron_expr: str, from_dt: datetime) -> datetime:
    """Devolve o próximo datetime em que a expressão cron dispara, após `from_dt`."""
    if _HAS_CRONITER:
        itr = _croniter(cron_expr, from_dt)
        return itr.get_next(datetime)
    return _fallback_next(cron_expr, from_dt)


def is_due(cron_expr: str, last_run: datetime | None, now: datetime) -> bool:
    """True se a tarefa deve rodar agora, dado o último timestamp de execução.

    Lógica: calcula o próximo disparo a partir de `last_run` (ou de 1 minuto
    atrás se nunca rodou) e compara com `now`. Se já passou (ou bateu),
    é hora de rodar.
    """
    base = last_run if last_run is not None else now - timedelta(minutes=1)
    try:
        nxt = next_run(cron_expr, base)
    except Exception:
        return False
    return nxt <= now


# ----------------------------------------------------------------------
# Fallback: parser mínimo de cron
# ----------------------------------------------------------------------
def _fallback_next(cron_expr: str, from_dt: datetime) -> datetime:
    """Implementação minimalista de "próximo disparo" para o subset suportado."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"expressão cron inválida (esperado 5 campos): {cron_expr!r}"
        )
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    # day-of-month e month não suportados no fallback (assumimos *).
    if dom_f != "*" or month_f != "*":
        raise ValueError(
            "fallback cron só suporta '*' em dia-do-mês e mês "
            "(instale `croniter` pra suporte completo)"
        )

    minute_set = _parse_field(minute_f, 0, 59)
    hour_set = _parse_field(hour_f, 0, 23)
    dow_set = _parse_field(dow_f, 0, 6)

    # Procura o próximo minuto válido a partir de `from_dt + 1min`.
    candidate = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Limite de busca: 1 ano (paranoia anti loop infinito).
    horizon = candidate + timedelta(days=366)
    while candidate <= horizon:
        # Cron usa 0=domingo; Python weekday() usa 0=segunda.
        py_dow = candidate.weekday()
        cron_dow = (py_dow + 1) % 7
        if (
            candidate.minute in minute_set
            and candidate.hour in hour_set
            and cron_dow in dow_set
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"nenhum disparo encontrado em 1 ano para {cron_expr!r}")


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    """Parseia um campo único de cron pra um set de inteiros válidos.

    Suporta:
        "*"          -> todos os valores no range
        "*/N"        -> múltiplos de N
        "A-B"        -> range A..B
        "A,B,C"      -> lista
        "A"          -> valor único
    """
    field = field.strip()
    if field == "*":
        return set(range(lo, hi + 1))

    # Lista separada por vírgula: aplica recursivamente em cada parte.
    if "," in field:
        out: set[int] = set()
        for part in field.split(","):
            out.update(_parse_field(part, lo, hi))
        return out

    # Step: */N ou A-B/N
    if "/" in field:
        base, step_str = field.split("/", 1)
        try:
            step = int(step_str)
        except ValueError as e:
            raise ValueError(f"step inválido em {field!r}") from e
        if step <= 0:
            raise ValueError(f"step deve ser > 0 em {field!r}")
        base_set = _parse_field(base if base else "*", lo, hi)
        return {v for v in base_set if (v - lo) % step == 0}

    # Range A-B
    if "-" in field:
        a_str, b_str = field.split("-", 1)
        try:
            a, b = int(a_str), int(b_str)
        except ValueError as e:
            raise ValueError(f"range inválido em {field!r}") from e
        if a > b or a < lo or b > hi:
            raise ValueError(f"range fora dos limites em {field!r}")
        return set(range(a, b + 1))

    # Valor único
    try:
        v = int(field)
    except ValueError as e:
        raise ValueError(f"valor inválido em {field!r}") from e
    if v < lo or v > hi:
        raise ValueError(f"valor {v} fora do range [{lo}, {hi}]")
    return {v}
