from dataclasses import dataclass
from typing import Dict, Literal

DrugName = Literal[
    "semaglutide",
    "tirzepatide",
    "retatrutide",
    "cagrilintide",
    "survodutide",
    "mazdutide",
]

@dataclass(frozen=True)
class DrugParams:
    name: str
    t_half_days: float
    F: float
    tmax_h: float | None = None


PEPTIDE_DATA: Dict[DrugName, DrugParams] = {
    "semaglutide": DrugParams(
        name="Семаглутид",
        t_half_days=7.0,
        F=0.89,
        tmax_h=48.0,
    ),
    "tirzepatide": DrugParams(
        name="Тирзепатид",
        t_half_days=5.5,
        F=0.80,
        tmax_h=36.0,
    ),
    "retatrutide": DrugParams(
        name="Ретатрутид",
        t_half_days=6.0,
        F=0.80,
        tmax_h=24.0,
    ),
    "cagrilintide": DrugParams(
        name="Кагрилинтид",
        t_half_days=8,
        F=0.8,
        tmax_h=48.0,
    ),
    "survodutide": DrugParams(
        name="Сурводутид",
        t_half_days=6,
        F=0.80,
        tmax_h=30.0,
    ),
    "mazdutide": DrugParams(
        name="Маздутид",
        t_half_days=8,
        F=0.8,
        tmax_h=72.0,
    ),
}
