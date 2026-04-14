import math


def _get(drug, name: str, default=None): return getattr(drug, name, default)
def _require_float(drug, *names: str) -> float:
    for name in names:
        value = getattr(drug, name, None)
        if value is None: continue
        value = float(value)
        if value > 0: return value
    raise ValueError(f"Missing required positive float field in drugparams: one of {names}")

def solve_ka_from_tmax_ke(tmax_days: float, ke: float) -> float:
    if tmax_days <= 0: raise ValueError("tmax_days must be > 0")
    if ke <= 0: raise ValueError("ke must be > 0")
    lo = ke * 1.0001
    hi = 100.0
    def f(ka: float) -> float: return math.log(ka / ke) / (ka - ke) - tmax_days
    while f(hi) > 0:
        hi *= 2.0
        if hi > 1e6: break

    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0: lo = mid
        else: hi = mid

    return 0.5 * (lo + hi)


def amount_single_dose(t_days: float, dose_mg: float, F: float, ka: float, ke: float) -> float:
    if t_days < 0: return 0.0
    if abs(ka - ke) < 1e-12: return F * dose_mg * (ka * t_days) * math.exp(-ke * t_days)
    return F * dose_mg * (ka / (ka - ke)) * (math.exp(-ke * t_days) - math.exp(-ka * t_days))
