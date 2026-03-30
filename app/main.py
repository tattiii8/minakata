import os
import httpx
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, HTTPException
from apscheduler.schedulers.asyncio import AsyncIOScheduler

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
NOTIFY_CITY = os.getenv("NOTIFY_CITY", "Tokyo")

scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")


async def send_daily_forecast():
    """毎朝8時に天気を通知"""
    result = await forecast(city=NOTIFY_CITY)
    await send_weather_message(
        city=result["city"],
        forecast=result["forecast"],
    )


@asynccontextmanager
async def lifespan(app):
    scheduler.add_job(send_daily_forecast, "cron", hour=8, minute=0)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="minakata", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/forecast")
async def forecast(city: str, days: int = 7):
    """都市名を指定して天気予報を取得"""

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:

        # 1. 都市名 → 緯度経度
        try:
            geo_res = await client.get(GEOCODING_URL, params={
                "name": city,
                "count": 1,
                "language": "ja",
            })
        except httpx.ConnectTimeout:
            raise HTTPException(status_code=504, detail="ジオコーディングAPIへの接続がタイムアウトしました")

        geo_data = geo_res.json()

        if not geo_data.get("results"):
            raise HTTPException(status_code=404, detail=f"都市が見つかりません: {city}")

        location = geo_data["results"][0]
        lat = location["latitude"]
        lon = location["longitude"]
        city_name = location["name"]
        country = location["country"]

        # 2. 天気予報取得
        try:
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
        except httpx.ConnectTimeout:
            raise HTTPException(status_code=504, detail="天気予報APIへの接続がタイムアウトしました")

    daily = weather_res.json()["daily"]

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


async def send_weather_message(city: str, forecast: list):
    """LINEに天気を送信"""

    if not LINE_ACCESS_TOKEN or not LINE_USER_ID:
        return

    today = forecast[0]
    tomorrow = forecast[1]

    text = (
        f"🌤 {city}の天気予報\n"
        f"\n"
        f"【今日 {today['date']}】\n"
        f"{today['weather']} {today['temp_min']}℃ / {today['temp_max']}℃\n"
        f"降水量: {today['precipitation_mm']}mm  風速: {today['windspeed_kmh']}km/h\n"
        f"\n"
        f"【明日 {tomorrow['date']}】\n"
        f"{tomorrow['weather']} {tomorrow['temp_min']}℃ / {tomorrow['temp_max']}℃\n"
        f"降水量: {tomorrow['precipitation_mm']}mm  風速: {tomorrow['windspeed_kmh']}km/h\n"
    )

    headers = {
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    body = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(LINE_API_URL, headers=headers, json=body)
        res.raise_for_status()


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