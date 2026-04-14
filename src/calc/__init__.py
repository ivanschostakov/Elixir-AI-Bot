import logging
import time

from .drugparams import DrugName, PEPTIDE_DATA
from .plotting import plot_filled_scale, save_single_plot
from .simulation import simulate_course_amount_only

graph_logger = logging.getLogger("ai.graph")


def generate_drug_graphs(drug_key: DrugName, weeks: float, dose_mg: float, interval_days: float) -> str:
    started_at = time.perf_counter()
    graph_logger.info(
        "Graph calc start | drug=%s | weeks=%s | dose_mg=%s | interval_days=%s",
        drug_key,
        weeks,
        dose_mg,
        interval_days,
    )

    times, amounts, base_label, info = simulate_course_amount_only(
        drug_key,
        dose_mg=dose_mg,
        weeks=weeks,
        interval_days=interval_days,
    )

    sanitized_name = str(drug_key).lower().replace(" ", "_")
    param_suffix = f"{dose_mg:g}mg_{weeks:g}wks_{interval_days:g}d.png"
    filename = f"plot_{sanitized_name}_amount_{param_suffix}"

    save_single_plot(
        x_data=times,
        y_data=amounts,
        title=f"Содержание вещества в организме (мг) — {PEPTIDE_DATA[drug_key].name}",
        y_label="Содержание, мг",
        legend_label=base_label,
        filename=filename,
        info=info,
    )

    graph_logger.info(
        "Graph calc done | drug=%s | filename=%s | elapsed_ms=%s",
        drug_key,
        filename,
        int((time.perf_counter() - started_at) * 1000),
    )
    return filename


__all__ = ["DrugName", "PEPTIDE_DATA", "generate_drug_graphs", "plot_filled_scale"]
