import math
from typing import Union

from .drugparams import DrugName, PEPTIDE_DATA
from .helpers import _get, _require_float, amount_single_dose, solve_ka_from_tmax_ke


def simulate_course_amount_only(drug_name_key: Union[DrugName, str], dose_mg: float, weeks: float, interval_days: float, dt: float = 0.05):
    if dose_mg <= 0: raise ValueError("dose_mg должно быть > 0")
    if weeks <= 0: raise ValueError("weeks должно быть > 0")
    if interval_days <= 0: raise ValueError("interval_days должно быть > 0")
    if dt <= 0: raise ValueError("dt должно быть > 0")

    drug = PEPTIDE_DATA[drug_name_key]
    t_half_days = _require_float(drug, "t_half_days")
    F = float(_get(drug, "F", 1.0))
    tmax_h = _get(drug, "tmax_h", None)

    if not (0 < F <= 1.0): raise ValueError(f"Invalid F for {drug_name_key}: {F}")

    ke = math.log(2) / t_half_days

    if tmax_h is not None and float(tmax_h) > 0: ka = solve_ka_from_tmax_ke(float(tmax_h) / 24.0, ke)
    else: ka = ke * 3.0

    n_doses = max(1, int(math.ceil((weeks * 7.0) / interval_days)))
    inj_times = [index * interval_days for index in range(n_doses)]
    t_end = inj_times[-1] + 5.0 * t_half_days

    times: list[float] = []
    amounts_mg: list[float] = []

    t = 0.0
    while t <= t_end + 1e-12:
        amount = sum(amount_single_dose(t_days=t - inj_t, dose_mg=dose_mg, F=F, ka=ka, ke=ke) for inj_t in inj_times)
        times.append(t)
        amounts_mg.append(amount)
        t += dt

    base_label = f"{_get(drug, 'name', str(drug_name_key))}: {dose_mg:g} мг каждые {interval_days:g} д"
    info = {
        "F": F,
        "t_half_days": t_half_days,
        "tmax_h": float(tmax_h) if tmax_h is not None else None,
        "ka": ka,
        "ke": ke,
        "weeks": weeks,
        "interval_days": interval_days,
        "dose_mg": dose_mg,
    }
    return times, amounts_mg, base_label, info
