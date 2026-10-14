"""Demand forecasting and order-safety logic for Smart Inventory."""

import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import calendar_context as cc
import database as db
import weather as wx

MIN_OBSERVATIONS = 2
_FORECAST_CACHE = {}
FORECAST_TTL_SECONDS = 60


def clear_cache():
    _FORECAST_CACHE.clear()


def _cache_get(key):
    item = _FORECAST_CACHE.get(key)
    if not item:
        return None
    saved_at, value = item
    if time.monotonic() - saved_at > FORECAST_TTL_SECONDS:
        _FORECAST_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key, value):
    _FORECAST_CACHE[key] = (time.monotonic(), value)
    return value


def _daily_total_sales():
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT quantity, timestamp FROM stock_movement WHERE movement_type='sale'"
        ).fetchall()
    daily = defaultdict(float)
    for row in rows:
        d = datetime.strptime(str(row["timestamp"]), "%Y-%m-%d %H:%M").date()
        daily[d] += abs(float(row["quantity"]))
    return daily


def _product_sales_totals():
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT product_id, SUM(ABS(quantity)) AS total
            FROM stock_movement
            WHERE movement_type='sale'
            GROUP BY product_id
            """
        ).fetchall()
    return {int(r["product_id"]): float(r["total"] or 0) for r in rows}


def compute_seasonal_multipliers(location=cc.DEFAULT_LOCATION, daily_sales=None):
    lat, lon, state = cc.LOCATION_PRESETS.get(
        location, cc.LOCATION_PRESETS[cc.DEFAULT_LOCATION]
    )
    if daily_sales is None:
        daily_sales = _daily_total_sales()
    if not daily_sales:
        return {}, {}, {
            "days_of_data": 0,
            "weather_days_matched": 0,
            "observations_per_flag": {},
        }

    dates = sorted(daily_sales)
    hist_weather = wx.get_historical_weather(dates[0], dates[-1], lat, lon)
    samples = {
        "weekend": {"with": [], "without": []},
        "public_holiday": {"with": [], "without": []},
        "school_holiday": {"with": [], "without": []},
        "rainy": {"with": [], "without": []},
        "extreme_heat": {"with": [], "without": []},
    }
    monthly = defaultdict(list)

    for d in dates:
        total = daily_sales[d]
        monthly[d.month].append(total)
        context = cc.day_context(d, state)
        samples["weekend"]["with" if context["is_weekend"] else "without"].append(total)
        samples["public_holiday"]["with" if context["is_public_holiday"] else "without"].append(total)
        if context.get("school_holiday_data_available"):
            samples["school_holiday"]["with" if context["is_school_holiday"] else "without"].append(total)
        weather = hist_weather.get(d.isoformat())
        if weather:
            samples["rainy"]["with" if weather.get("is_rainy") else "without"].append(total)
            samples["extreme_heat"]["with" if weather.get("is_extreme_heat") else "without"].append(total)

    multipliers = {}
    observation_meta = {}
    for flag, sides in samples.items():
        with_values = sides["with"]
        without_values = sides["without"]
        observation_meta[flag] = {"with": len(with_values), "without": len(without_values)}
        if len(with_values) >= MIN_OBSERVATIONS and len(without_values) >= MIN_OBSERVATIONS:
            with_avg = sum(with_values) / len(with_values)
            without_avg = sum(without_values) / len(without_values)
            multipliers[flag] = round(with_avg / without_avg, 2) if without_avg else 1.0
        else:
            multipliers[flag] = 1.0

    all_avg = sum(daily_sales.values()) / len(daily_sales)
    monthly_multipliers = {}
    if len(monthly) >= 2 and all_avg:
        for month_num, values in monthly.items():
            if len(values) >= 5:
                monthly_multipliers[month_num] = round((sum(values) / len(values)) / all_avg, 2)

    return multipliers, monthly_multipliers, {
        "days_of_data": len(dates),
        "weather_days_matched": len(hist_weather),
        "observations_per_flag": observation_meta,
    }


def _dominant_effect(context, day_weather, multipliers, month_multiplier=1.0):
    # Public holidays have highest priority. For the other special conditions,
    # use the strongest learned effect instead of multiplying them together.
    if context.get("is_public_holiday"):
        return "Public holiday", float(multipliers.get("public_holiday", 1.0))

    candidates = {}
    if context.get("is_school_holiday"):
        candidates["School holiday"] = multipliers.get("school_holiday", 1.0)
    if context.get("is_weekend"):
        candidates["Weekend"] = multipliers.get("weekend", 1.0)
    if day_weather.get("is_rainy"):
        candidates["Rain"] = multipliers.get("rainy", 1.0)
    if day_weather.get("is_extreme_heat"):
        candidates["Extreme heat"] = multipliers.get("extreme_heat", 1.0)

    if candidates:
        label = max(candidates, key=lambda k: abs(candidates[k] - 1.0))
        return label, float(candidates[label])

    if month_multiplier != 1.0:
        return "Monthly pattern", float(month_multiplier)

    return "Normal day", 1.0


def _confidence(days):
    if days < 7:
        return "Very low"
    if days < 14:
        return "Low"
    if days < 30:
        return "Developing"
    if days < 90:
        return "Moderate"
    return "Good"


def get_upcoming_events(location=cc.DEFAULT_LOCATION, days_ahead=7):
    lat, lon, state = cc.LOCATION_PRESETS.get(
        location, cc.LOCATION_PRESETS[cc.DEFAULT_LOCATION]
    )
    daily_sales = _daily_total_sales()
    multipliers, monthly_multipliers, meta = compute_seasonal_multipliers(
        location, daily_sales=daily_sales
    )
    weather_map = wx.get_forecast_weather(max(days_ahead, 1), lat, lon)

    rows = []
    today = date.today()
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        context = cc.day_context(d, state)
        weather = weather_map.get(d.isoformat(), {})
        month_multiplier = monthly_multipliers.get(d.month, 1.0)
        factor, multiplier = _dominant_effect(context, weather, multipliers, month_multiplier)

        conditions = []
        if context.get("is_public_holiday"):
            conditions.append(context.get("public_holiday_name") or "Public holiday")
        if context.get("is_school_holiday"):
            conditions.append("School holiday")
        if context.get("is_weekend"):
            conditions.append("Weekend")
        if weather.get("is_rainy"):
            conditions.append("Rain")
        if weather.get("is_extreme_heat"):
            conditions.append("Extreme heat")
        if not conditions:
            conditions.append("Normal trading day")

        pct = round((multiplier - 1.0) * 100)
        if pct > 0:
            impact = f"~{pct}% higher than baseline"
        elif pct < 0:
            impact = f"~{abs(pct)}% lower than baseline"
        else:
            impact = "Baseline demand"

        rows.append({
            "date": d,
            "day": d.strftime("%A"),
            "conditions": ", ".join(conditions),
            "rain_mm": weather.get("rain_mm"),
            "max_temp_c": weather.get("max_temp_c"),
            "factor_used": factor,
            "multiplier": round(multiplier, 2),
            "impact": impact,
        })

    meta.update({
        "multipliers": multipliers,
        "monthly_multipliers": monthly_multipliers,
        "weather_available": bool(weather_map),
        "confidence": _confidence(meta.get("days_of_data", 0)),
    })
    return rows, meta


def predict_reorder_quantities(location=cc.DEFAULT_LOCATION, days_ahead=7, force_refresh=False):
    cache_key = (location, int(days_ahead), date.today().isoformat())
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    lat, lon, state = cc.LOCATION_PRESETS.get(
        location, cc.LOCATION_PRESETS[cc.DEFAULT_LOCATION]
    )
    daily_sales = _daily_total_sales()
    multipliers, monthly_multipliers, meta = compute_seasonal_multipliers(
        location, daily_sales=daily_sales
    )
    forecast_weather = wx.get_forecast_weather(max(days_ahead, 1), lat, lon)
    observed_sales_days = len(daily_sales)
    inventory = db.get_manager_inventory()
    total_map = _product_sales_totals()
    on_order = db.get_on_order_quantities()

    today = date.today()
    forecast_days = [today + timedelta(days=i) for i in range(days_ahead)]
    results = []

    for product in inventory:
        product_id = int(product["product_id"])
        total_sold = total_map.get(product_id, 0.0)
        baseline = total_sold / observed_sales_days if observed_sales_days else 0.0

        daily_forecast = []
        for d in forecast_days:
            context = cc.day_context(d, state)
            weather = forecast_weather.get(d.isoformat(), {})
            month_multiplier = monthly_multipliers.get(d.month, 1.0)
            factor, multiplier = _dominant_effect(
                context, weather, multipliers, month_multiplier
            )
            predicted = baseline * multiplier
            daily_forecast.append({
                "date": d.isoformat(),
                "day": d.strftime("%A"),
                "predicted_demand": round(predicted, 2),
                "factor_used": factor,
                "multiplier": round(multiplier, 2),
                "public_holiday": context.get("public_holiday_name"),
                "school_holiday": bool(context.get("is_school_holiday")),
                "weekend": bool(context.get("is_weekend")),
                "rain_mm": weather.get("rain_mm"),
                "max_temp_c": weather.get("max_temp_c"),
            })

        today_demand = daily_forecast[0]["predicted_demand"] if daily_forecast else 0.0
        week_demand = sum(d["predicted_demand"] for d in daily_forecast)
        current = float(product["current_stock"])
        incoming = int(on_order.get(product_id, 0))
        safety = int(product.get("reorder_threshold") or 0)
        usual = int(product.get("usual_order_qty") or 0)
        max_stock = product.get("max_stock_level")
        max_stock = int(max_stock) if max_stock not in (None, "") else None

        # Forecast need before applying the stock-cap rule.
        forecast_required = max(0, math.ceil(week_demand + safety - current - incoming))
        projected_after_usual = current + incoming + usual - week_demand
        shortfall = max(0, math.ceil(safety - projected_after_usual))

        # Maximum stock is a hard cap on how much can be held after the next
        # delivery arrives. Existing open orders are counted before suggesting
        # any additional quantity.
        max_safe_order = None
        stock_above_max = 0
        overorder_excess = 0
        if max_stock is not None:
            max_safe_order = max(0, math.floor(max_stock - current - incoming))
            stock_above_max = max(0, math.ceil(current + incoming - max_stock))
            overorder_excess = max(0, usual - max_safe_order)

        recommended_need = forecast_required
        if max_safe_order is not None:
            recommended_need = min(forecast_required, max_safe_order)

        unit_name = product.get("unit") or "units"
        if stock_above_max > 0:
            usual_status = "overorder"
            reason = (
                f"Current stock plus existing orders exceed maximum stock by "
                f"{stock_above_max} {unit_name}. Do not place another order."
            )
            recommended_final = 0
        elif max_safe_order == 0 and forecast_required > 0:
            usual_status = "overorder"
            reason = "No additional order recommended because the configured maximum stock is already covered."
            recommended_final = 0
        elif usual == 0 and recommended_need > 0:
            usual_status = "not_configured"
            if max_safe_order is not None and forecast_required > recommended_need:
                reason = (
                    f"Forecast suggests {forecast_required} {unit_name}, but maximum stock allows only "
                    f"{recommended_need} more. Usual weekly order is not set."
                )
            else:
                reason = "Usual weekly order not set."
            recommended_final = recommended_need
        elif shortfall > 0:
            usual_status = "shortfall"
            if max_safe_order is not None and forecast_required > recommended_need:
                reason = (
                    f"Forecast suggests {forecast_required} {unit_name}, but maximum stock allows only "
                    f"{recommended_need} more."
                )
            else:
                reason = f"Usual order is short by {shortfall} {unit_name}."
            recommended_final = recommended_need
        elif overorder_excess > 0:
            usual_status = "overorder"
            reason = f"Usual order may exceed maximum stock by {overorder_excess} {unit_name}."
            recommended_final = max_safe_order
        else:
            usual_status = "good"
            reason = "Usual order should cover expected demand without exceeding the configured maximum."
            recommended_final = usual

        results.append({
            "product_id": product_id,
            "name": product["product_name"],
            "category": product.get("category") or "Other",
            "unit": product.get("unit") or "units",
            "supplier_name": product.get("supplier_name") or "No supplier",
            "supplier_id": product.get("supplier_id"),
            "current_stock": current,
            "safety_stock": safety,
            "usual_order_qty": usual,
            "max_stock_level": max_stock,
            "shelf_life_days": product.get("shelf_life_days"),
            "on_order_qty": incoming,
            "baseline_daily_avg": round(baseline, 2),
            "predicted_today_demand": round(float(today_demand), 1),
            "predicted_week_demand": round(float(week_demand), 1),
            "projected_stock_after_usual": round(projected_after_usual, 1),
            "required_order_qty": forecast_required,
            "usual_order_shortfall": shortfall,
            "max_safe_order_qty": max_safe_order,
            "stock_above_max": stock_above_max,
            "overorder_excess": overorder_excess,
            "usual_order_status": usual_status,
            "recommended_final_order_qty": int(recommended_final or 0),
            "reason": reason,
            "daily_forecast": daily_forecast,
        })

    priority = {"shortfall": 0, "not_configured": 1, "overorder": 2, "good": 3}
    results.sort(key=lambda r: (priority.get(r["usual_order_status"], 9), r["name"]))

    attention = [r for r in results if r["usual_order_status"] != "good"]
    meta.update({
        "weather_available": bool(forecast_weather),
        "multipliers": multipliers,
        "monthly_multipliers": monthly_multipliers,
        "location": location,
        "confidence": _confidence(meta.get("days_of_data", 0)),
        "all_usual_orders_ok": len(attention) == 0 and len(results) > 0,
        "attention_count": len(attention),
    })

    return _cache_set(cache_key, (results, meta))
