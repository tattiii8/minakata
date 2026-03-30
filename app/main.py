import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(title="minakata", version="0.1.0")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/forecast")
async def forecast(city: str, days: int = 7):
    """都市名を指定して天気予報を取得"""

    # 1. 都市名 → 緯度経度
    async with httpx.AsyncClient() as client:
        geo_res = await client.get(GEOCODING_URL, params={
            "name": city,
            "count": 1,
            "language": "ja",
        })
        geo_data = geo_res.json()

    if not geo_data.get("results"):
        raise HTTPException(status_code=404, detail=f"都市が見つかりません: {city}")

    location = geo_data["results"][0]
    lat = location["latitude"]
    lon = location["longitude"]
    city_name = location["name"]
    country = location["country"]

    # 2. 天気予報取得
    async with httpx.AsyncClient() as client:
        weather_res = await client.get(OPEN_METEO_URL, params={
            "latitude": lat,
            "longitude": lon,
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "windspeed_10m_max",
                "weathercode",
            ],
            "timezone": "Asia/Tokyo",
            "forecast_days": days,
        })
        weather_data = weather_res.json()

    daily = weather_data["daily"]

    forecast_list = []
    for i in range(days):
        forecast_list.append({
            "date": daily["time"][i],
            "weather": _weather_label(daily["weathercode"][i]),
            "temp_max": daily["temperature_2m_max"][i],
            "temp_min": daily["temperature_2m_min"][i],
            "precipitation_mm": daily["precipitation_sum"][i],
            "windspeed_kmh": daily["windspeed_10m_max"][i],
        })

    return {
        "city": city_name,
        "country": country,
        "latitude": lat,
        "longitude": lon,
        "forecast": forecast_list,
    }


def _weather_label(code: int) -> str:
    if code == 0:
        return "快晴"
    elif code <= 3:
        return "晴れ〜曇り"
    elif code <= 49:
        return "霧"
    elif code <= 69:
        return "雨"
    elif code <= 79:
        return "雪"
    elif code <= 99:
        return "雷雨"
    return "不明"