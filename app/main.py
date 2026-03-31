import os
import sys
import logging
import httpx
import uvicorn

from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from apscheduler.schedulers.asyncio import AsyncIOScheduler

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
NOTIFY_CITY = os.getenv("NOTIFY_CITY", "Tokyo")

scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")


def setup_logging():
    root_level = os.getenv("LOG_LEVEL", "DEBUG").upper()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(root_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("uvicorn").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.error").setLevel(logging.DEBUG)
    logging.getLogger("uvicorn.access").setLevel(logging.DEBUG)


setup_logging()
logger = logging.getLogger("minakata")


async def send_daily_forecast():
    logger.info("scheduled daily forecast started city=%s", NOTIFY_CITY)

    try:
        result = await forecast(city=NOTIFY_CITY)
        await send_weather_message(
            city=result["city"],
            forecast=result["forecast"],
        )
        logger.info("scheduled daily forecast completed city=%s", result["city"])
    except Exception:
        logger.exception("scheduled daily forecast failed city=%s", NOTIFY_CITY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("application startup begin")
    logger.info("scheduler register daily forecast job at 08:00 Asia/Tokyo")

    scheduler.add_job(
        send_daily_forecast,
        "cron",
        hour=8,
        minute=0,
        id="daily_forecast_job",
        replace_existing=True,
    )
    scheduler.start()

    logger.info("scheduler started")
    yield

    logger.info("application shutdown begin")
    scheduler.shutdown()
    logger.info("scheduler stopped")


app = FastAPI(title="minakata", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    now = datetime.utcnow().isoformat()
    logger.debug("health check requested timestamp=%s", now)
    return {"status": "ok", "timestamp": now}


@app.post("/notify/test")
async def notify_test():
    logger.info("notify test endpoint called")
    await send_daily_forecast()
    logger.info("notify test completed")
    return {"status": "sent"}


@app.get("/forecast")
async def forecast(city: str, days: int = 7):
    logger.info("forecast start city=%s days=%s", city, days)

    if days < 1 or days > 16:
        logger.warning("invalid forecast days city=%s days=%s", city, days)
        raise HTTPException(status_code=400, detail="daysは1〜16で指定してください")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            geo_params = {
                "name": city,
                "count": 1,
                "language": "ja",
            }
            logger.debug("geocoding request url=%s params=%s", GEOCODING_URL, geo_params)

            geo_res = await client.get(GEOCODING_URL, params=geo_params)

            logger.info("geocoding response status=%s city=%s", geo_res.status_code, city)
            logger.debug("geocoding response body=%s", geo_res.text)

            geo_res.raise_for_status()

        except httpx.ConnectTimeout:
            logger.exception("geocoding connect timeout city=%s", city)
            raise HTTPException(
                status_code=504,
                detail="ジオコーディングAPIへの接続がタイムアウトしました",
            )
        except httpx.HTTPStatusError:
            logger.exception("geocoding http status error city=%s", city)
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIでエラーが発生しました",
            )
        except httpx.RequestError:
            logger.exception("geocoding request error city=%s", city)
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIへのリクエストに失敗しました",
            )

        try:
            geo_data = geo_res.json()
            logger.debug("geocoding parsed json=%s", geo_data)
        except Exception:
            logger.exception("failed to parse geocoding response city=%s", city)
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIのレスポンス解析に失敗しました",
            )

        if not geo_data.get("results"):
            logger.warning("city not found city=%s", city)
            raise HTTPException(status_code=404, detail=f"都市が見つかりません: {city}")

        location = geo_data["results"][0]
        lat = location["latitude"]
        lon = location["longitude"]
        city_name = location["name"]
        country = location["country"]

        logger.info(
            "geocoding success input_city=%s resolved_city=%s country=%s lat=%s lon=%s",
            city,
            city_name,
            country,
            lat,
            lon,
        )

        try:
            weather_params = {
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
            }
            logger.debug("weather request url=%s params=%s", OPEN_METEO_URL, weather_params)

            weather_res = await client.get(OPEN_METEO_URL, params=weather_params)

            logger.info(
                "weather response status=%s city=%s resolved_city=%s",
                weather_res.status_code,
                city,
                city_name,
            )
            logger.debug("weather response body=%s", weather_res.text)

            weather_res.raise_for_status()

        except httpx.ConnectTimeout:
            logger.exception("weather api connect timeout city=%s resolved_city=%s", city, city_name)
            raise HTTPException(
                status_code=504,
                detail="天気予報APIへの接続がタイムアウトしました",
            )
        except httpx.HTTPStatusError:
            logger.exception("weather api status error city=%s resolved_city=%s", city, city_name)
            raise HTTPException(
                status_code=502,
                detail="天気予報APIでエラーが発生しました",
            )
        except httpx.RequestError:
            logger.exception("weather api request error city=%s resolved_city=%s", city, city_name)
            raise HTTPException(
                status_code=502,
                detail="天気予報APIへのリクエストに失敗しました",
            )

    try:
        weather_data = weather_res.json()
        logger.debug("weather parsed json=%s", weather_data)
        daily = weather_data["daily"]
    except Exception:
        logger.exception("failed to parse weather response city=%s resolved_city=%s", city, city_name)
        raise HTTPException(
            status_code=502,
            detail="天気予報APIのレスポンス解析に失敗しました",
        )

    forecast_list = []
    try:
        for i in range(days):
            forecast_item = {
                "date": daily["time"][i],
                "weather": _weather_label(daily["weathercode"][i]),
                "temp_max": daily["temperature_2m_max"][i],
                "temp_min": daily["temperature_2m_min"][i],
                "precipitation_mm": daily["precipitation_sum"][i],
                "windspeed_kmh": daily["windspeed_10m_max"][i],
            }
            forecast_list.append(forecast_item)

        logger.info(
            "forecast success city=%s resolved_city=%s days=%s first_date=%s",
            city,
            city_name,
            days,
            forecast_list[0]["date"] if forecast_list else None,
        )
        logger.debug("forecast result=%s", forecast_list)

    except Exception:
        logger.exception("failed building forecast list city=%s resolved_city=%s", city, city_name)
        raise HTTPException(
            status_code=502,
            detail="天気予報データの整形に失敗しました",
        )

    return {
        "city": city_name,
        "country": country,
        "latitude": lat,
        "longitude": lon,
        "forecast": forecast_list,
    }


async def send_weather_message(city: str, forecast: list):
    logger.info("send_weather_message start city=%s", city)

    if not LINE_ACCESS_TOKEN:
        logger.warning("LINE_ACCESS_TOKEN is not set. skip broadcast city=%s", city)
        return

    if len(forecast) < 2:
        logger.error("forecast data is insufficient for broadcast city=%s forecast=%s", city, forecast)
        return

    today = forecast[0]
    tomorrow = forecast[1]

    text = (
        f"{city}の天気予報\n"
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
        "messages": [{"type": "text", "text": text}],
    }

    logger.debug("LINE broadcast request body=%s", body)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.post(
                "https://api.line.me/v2/bot/message/broadcast",
                headers=headers,
                json=body,
            )
            logger.info("LINE broadcast response status=%s city=%s", res.status_code, city)
            logger.debug("LINE broadcast response body=%s", res.text)
            res.raise_for_status()
            logger.info("send_weather_message success city=%s", city)

        except httpx.HTTPStatusError:
            logger.exception("LINE broadcast status error city=%s", city)
            raise
        except httpx.RequestError:
            logger.exception("LINE broadcast request error city=%s", city)
            raise
        except Exception:
            logger.exception("LINE broadcast unexpected error city=%s", city)
            raise


@app.post("/webhook")
async def webhook(request: Request):
    logger.info("webhook received")

    try:
        body = await request.json()
        logger.debug("webhook body=%s", body)
    except Exception:
        logger.exception("failed to parse webhook body as json")
        raise HTTPException(status_code=400, detail="不正なJSONです")

    events = body.get("events", [])
    logger.info("webhook events count=%s", len(events))

    for idx, event in enumerate(events):
        logger.debug("webhook event[%s]=%s", idx, event)

        if event.get("type") != "message":
            logger.debug("skip non-message event index=%s type=%s", idx, event.get("type"))
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            logger.debug("skip non-text message index=%s message_type=%s", idx, message.get("type"))
            continue

        reply_token = event.get("replyToken")
        text = message.get("text", "").strip()

        logger.info(
            "processing webhook message index=%s reply_token_exists=%s text=%s",
            idx,
            bool(reply_token),
            text,
        )

        try:
            await handle_message(reply_token, text)
            logger.info("webhook message processed index=%s text=%s", idx, text)
        except Exception:
            logger.exception("webhook message processing failed index=%s text=%s", idx, text)

    return {"status": "ok"}


async def handle_message(reply_token: str, text: str):
    logger.info("handle_message start text=%s", text)

    city = text if text else NOTIFY_CITY
    logger.info("resolved request city=%s", city)

    try:
        result = await forecast(city=city)
        today = result["forecast"][0]
        tomorrow = result["forecast"][1]

        reply_text = (
            f"🌤 {result['city']}の天気予報\n"
            f"\n"
            f"【今日 {today['date']}】\n"
            f"{today['weather']} {today['temp_min']}℃ / {today['temp_max']}℃\n"
            f"降水量: {today['precipitation_mm']}mm  風速: {today['windspeed_kmh']}km/h\n"
            f"\n"
            f"【明日 {tomorrow['date']}】\n"
            f"{tomorrow['weather']} {tomorrow['temp_min']}℃ / {tomorrow['temp_max']}℃\n"
            f"降水量: {tomorrow['precipitation_mm']}mm  風速: {tomorrow['windspeed_kmh']}km/h\n"
        )

        logger.info("handle_message forecast success city=%s resolved_city=%s", city, result["city"])

    except HTTPException as e:
        logger.warning(
            "handle_message forecast failed city=%s status_code=%s detail=%s",
            city,
            e.status_code,
            e.detail,
        )
        reply_text = (
            f"「{text}」の天気情報が見つかりませんでした。\n"
            f"都市名を送ってください。\n"
            f"例: 鎌倉、東京、Osaka"
        )
    except Exception:
        logger.exception("handle_message unexpected error city=%s", city)
        reply_text = "エラーが発生しました。しばらくしてからもう一度お試しください。"

    await send_reply(reply_token, reply_text)
    logger.info("handle_message reply sent text=%s", text)


async def send_reply(reply_token: str, text: str):
    logger.info("send_reply start reply_token_exists=%s", bool(reply_token))

    if not LINE_ACCESS_TOKEN:
        logger.warning("LINE_ACCESS_TOKEN is not set. skip reply")
        return

    headers = {
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }

    logger.debug("LINE reply request body=%s", body)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.post(
                "https://api.line.me/v2/bot/message/reply",
                headers=headers,
                json=body,
            )
            logger.info("LINE reply response status=%s", res.status_code)
            logger.debug("LINE reply response body=%s", res.text)
            res.raise_for_status()
            logger.info("send_reply success")

        except httpx.HTTPStatusError:
            logger.exception("LINE reply status error")
            raise
        except httpx.RequestError:
            logger.exception("LINE reply request error")
            raise
        except Exception:
            logger.exception("LINE reply unexpected error")
            raise


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


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="debug",
        access_log=True,
    )