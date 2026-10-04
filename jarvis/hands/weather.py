"""Weather via Open-Meteo (free, no key)."""

from __future__ import annotations

from .registry import ToolError, tool
from .web import client

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers",
       82: "violent showers", 95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


async def _locate(city: str) -> tuple[float, float, str]:
    if city:
        r = await client().get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": city, "count": 1})
        res = (r.json() or {}).get("results") or []
        if not res:
            raise ToolError(f"Unknown place '{city}'.")
        g = res[0]
        return g["latitude"], g["longitude"], f"{g['name']}, {g.get('country', '')}".strip(", ")
    # The user's real position when Jarvis has one (phone GPS / this device), else the internet connection's city.
    import asyncio

    from .. import geo

    p = await asyncio.to_thread(geo.here, False, True)
    if not p:
        raise ToolError("Couldn't detect your location.", hint="Pass a city name.")
    return p["lat"], p["lon"], (p.get("city") or "your area") if p.get("approx") else "your location"


@tool(risk="low", tags=["weather", "temperature", "forecast", "rain", "hot", "cold", "outside"],
      examples=["get_weather(city='London')", "get_weather()"])
async def get_weather(city: str = "", days: int = 1, units: str = "celsius") -> dict:
    """Current weather and forecast for a city (or the user's location if empty)."""
    lat, lon, place = await _locate(city)
    r = await client().get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        "forecast_days": max(1, min(7, days)), "timezone": "auto",
        "temperature_unit": "fahrenheit" if units.lower().startswith("f") else "celsius",
    })
    j = r.json()
    cur = j["current"]
    d = j["daily"]
    return {
        "place": place,
        "now": {"temp": cur["temperature_2m"], "feels_like": cur["apparent_temperature"],
                "conditions": WMO.get(cur["weather_code"], "unknown"), "wind_kmh": cur["wind_speed_10m"],
                "humidity": cur["relative_humidity_2m"]},
        "forecast": [
            {"date": d["time"][i], "high": d["temperature_2m_max"][i], "low": d["temperature_2m_min"][i],
             "conditions": WMO.get(d["weather_code"][i], "unknown"),
             "rain_chance": d["precipitation_probability_max"][i]}
            for i in range(len(d["time"]))
        ],
        "units": "F" if units.lower().startswith("f") else "C",
    }
