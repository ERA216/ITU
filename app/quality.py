"""Честная оценка качества движка на нескольких наборах синтетических данных.

Запуск:  python -m app.quality
Пишет app/static/quality.json, который показывает страница /quality.
Каждый набор данных создаётся с другим зерном случайности, поэтому видно не одно удачное число, а разброс.
"""
import json
import os
import statistics
import tempfile
import time
from datetime import datetime

from . import clock, engine, seed

SEEDS = range(1, 11)
THRESHOLDS = list(range(40, 101, 5))
OUT = os.path.join(os.path.dirname(__file__), "static", "quality.json")

SCENARIOS = {
    "tri_out": ("Схема", "Переслал всю сумму одним переводом"),
    "tri_split": ("Схема", "Раздробил сумму на две части"),
    "tri_late": ("Схема", "Переслал через 3–4 дня"),
    "benign_supplier": ("Честный", "Получил оплату и заплатил знакомому поставщику"),
    "benign_new": ("Честный", "Получил от незнакомца и заплатил новому человеку"),
    "benign_return": ("Честный", "Вернул деньги тому же отправителю"),
    "normal": ("Честный", "Обычный перевод знакомому"),
}


def held(score: int, has_incoming: bool, threshold: int) -> bool:
    return has_incoming and score >= threshold


def run() -> dict:
    tmp = tempfile.mkdtemp()
    per_seed = []
    for s in SEEDS:
        conn, stats = seed.build(os.path.join(tmp, f"q{s}.db"), seed=s, verbose=False)
        conn.close()
        per_seed.append(stats["scored"])

    curve = []
    for t in THRESHOLDS:
        recall, false_rate, precision = [], [], []
        for scored in per_seed:
            scheme = [held(sc, inc, t) for sc, inc, scen in scored if scen.startswith("tri_")]
            honest = [held(sc, inc, t) for sc, inc, scen in scored if not scen.startswith("tri_")]
            caught, false = sum(scheme), sum(honest)
            recall.append(100 * caught / len(scheme))
            false_rate.append(100 * false / len(honest))
            precision.append(100 * caught / max(1, caught + false))
        curve.append({
            "threshold": t,
            "recall": [round(min(recall), 1), round(statistics.mean(recall), 1), round(max(recall), 1)],
            "false_rate": [round(min(false_rate), 2), round(statistics.mean(false_rate), 2), round(max(false_rate), 2)],
            "precision": [round(min(precision), 1), round(statistics.mean(precision), 1), round(max(precision), 1)],
        })

    by_scenario = []
    for key, (group, title) in SCENARIOS.items():
        rows = [(sc, inc) for scored in per_seed for sc, inc, scen in scored if scen == key]
        stopped = sum(held(sc, inc, engine.HOLD_THRESHOLD) for sc, inc in rows)
        by_scenario.append({"key": key, "group": group, "title": title, "total": len(rows), "stopped": stopped})

    return {
        "generated_at": clock.now().isoformat(),
        "seeds": len(SEEDS),
        "threshold": engine.HOLD_THRESHOLD,
        "transfers_per_seed": round(statistics.mean(len(s) for s in per_seed)),
        "curve": curve,
        "by_scenario": by_scenario,
    }


if __name__ == "__main__":
    started = time.time()
    data = run()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    cur = next(c for c in data["curve"] if c["threshold"] == data["threshold"])
    print(f"Наборов данных: {data['seeds']}, порог {data['threshold']}, {time.time() - started:.0f} с")
    print(f"Ловит схем: {cur['recall'][0]}–{cur['recall'][2]}% (в среднем {cur['recall'][1]}%)")
    print(f"Зря останавливает честных: {cur['false_rate'][0]}–{cur['false_rate'][2]}%")
    print(f"Среди остановок настоящих схем: {cur['precision'][0]}–{cur['precision'][2]}%")
    for r in data["by_scenario"]:
        print(f"  {r['group']:8} {r['title']}: остановлено {r['stopped']} из {r['total']}")
