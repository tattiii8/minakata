import os
import sys
import json
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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HTTP_TRACE = os.getenv("HTTP_TRACE", "0") == "1"

scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "extra_data") and record.extra_data is not None:
            payload["data"] = record.extra_data

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(LOG_LEVEL)
    root_logger.addHandler(handler)

    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    if HTTP_TRACE:
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_event(logger, level, message, data=None, exc_info=False):
    getattr(logger, level)(
        message,
        extra={"extra_data": data},
        exc_info=exc_info,
    )


setup_logging()
logger = logging.getLogger("minakata")


async def send_daily_forecast():
    log_event(
        logger,
        "info",
        "scheduled_daily_forecast_started",
        {"city": NOTIFY_CITY},
    )

    try:
        result = await forecast(city=NOTIFY_CITY)
        await send_weather_message(
            city=result["city"],
            forecast=result["forecast"],
        )
        log_event(
            logger,
            "info",
            "scheduled_daily_forecast_completed",
            {"city": result["city"]},
        )
    except Exception:
        log_event(
            logger,
            "error",
            "scheduled_daily_forecast_failed",
            {"city": NOTIFY_CITY},
            exc_info=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_event(logger, "info", "application_startup_begin")
    log_event(
        logger,
        "info",
        "scheduler_register_job",
        {
            "job": "send_daily_forecast",
            "trigger": "cron",
            "hour": 8,
            "minute": 0,
            "timezone": "Asia/Tokyo",
        },
    )

    scheduler.add_job(
        send_daily_forecast,
        "cron",
        hour=8,
        minute=0,
        id="daily_forecast_job",
        replace_existing=True,
    )
    scheduler.start()

    log_event(logger, "info", "scheduler_started")
    yield

    log_event(logger, "info", "application_shutdown_begin")
    scheduler.shutdown()
    log_event(logger, "info", "scheduler_stopped")


app = FastAPI(title="minakata", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    now = datetime.utcnow().isoformat()
    log_event(
        logger,
        "debug",
        "health_check_requested",
        {"timestamp": now},
    )
    return {"status": "ok", "timestamp": now}


@app.post("/notify/test")
async def notify_test():
    log_event(logger, "info", "notify_test_called")
    await send_daily_forecast()
    log_event(logger, "info", "notify_test_completed")
    return {"status": "sent"}


@app.get("/forecast")
async def forecast(city: str, days: int = 7):
    log_event(
        logger,
        "info",
        "forecast_started",
        {"city": city, "days": days},
    )

    if days < 1 or days > 16:
        log_event(
            logger,
            "warning",
            "forecast_invalid_days",
            {"city": city, "days": days},
        )
        raise HTTPException(status_code=400, detail="daysは1〜16で指定してください")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            geo_params = {
                "name": city,
                "count": 1,
                "language": "ja",
            }
            log_event(
                logger,
                "debug",
                "geocoding_request",
                {"url": GEOCODING_URL, "params": geo_params},
            )

            geo_res = await client.get(GEOCODING_URL, params=geo_params)

            log_event(
                logger,
                "info",
                "geocoding_response",
                {"city": city, "status_code": geo_res.status_code},
            )

            geo_res.raise_for_status()

        except httpx.ConnectTimeout:
            log_event(
                logger,
                "error",
                "geocoding_connect_timeout",
                {"city": city},
                exc_info=True,
            )
            raise HTTPException(
                status_code=504,
                detail="ジオコーディングAPIへの接続がタイムアウトしました",
            )
        except httpx.HTTPStatusError:
            log_event(
                logger,
                "error",
                "geocoding_http_status_error",
                {"city": city},
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIでエラーが発生しました",
            )
        except httpx.RequestError:
            log_event(
                logger,
                "error",
                "geocoding_request_error",
                {"city": city},
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIへのリクエストに失敗しました",
            )

        try:
            geo_data = geo_res.json()
            log_event(
                logger,
                "debug",
                "geocoding_response_json",
                geo_data,
            )
        except Exception:
            log_event(
                logger,
                "error",
                "geocoding_json_parse_failed",
                {"city": city},
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="ジオコーディングAPIのレスポンス解析に失敗しました",
            )

        if not geo_data.get("results"):
            log_event(
                logger,
                "warning",
                "city_not_found",
                {"city": city},
            )
            raise HTTPException(status_code=404, detail=f"都市が見つかりません: {city}")

        location = geo_data["results"][0]
        lat = location["latitude"]
        lon = location["longitude"]
        city_name = location["name"]
        country = location["country"]

        log_event(
            logger,
            "info",
            "geocoding_success",
            {
                "input_city": city,
                "resolved_city": city_name,
                "country": country,
                "latitude": lat,
                "longitude": lon,
            },
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

            log_event(
                logger,
                "debug",
                "weather_request",
                {"url": OPEN_METEO_URL, "params": weather_params},
            )

            weather_res = await client.get(OPEN_METEO_URL, params=weather_params)

            log_event(
                logger,
                "info",
                "weather_response",
                {
                    "input_city": city,
                    "resolved_city": city_name,
                    "status_code": weather_res.status_code,
                },
            )

            weather_res.raise_for_status()

        except httpx.ConnectTimeout:
            log_event(
                logger,
                "error",
                "weather_connect_timeout",
                {"input_city": city, "resolved_city": city_name},
                exc_info=True,
            )
            raise HTTPException(
                status_code=504,
                detail="天気予報APIへの接続がタイムアウトしました",
            )
        except httpx.HTTPStatusError:
            log_event(
                logger,
                "error",
                "weather_http_status_error",
                {"input_city": city, "resolved_city": city_name},
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="天気予報APIでエラーが発生しました",
            )
        except httpx.RequestError:
            log_event(
                logger,
                "error",
                "weather_request_error",
                {"input_city": city, "resolved_city": city_name},
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail="天気予報APIへのリクエストに失敗しました",
            )

    try:
        weather_data = weather_res.json()
        log_event(
            logger,
            "debug",
            "weather_response_json",
            weather_data,
        )
        daily = weather_data["daily"]
    except Exception:
        log_event(
            logger,
            "error",
            "weather_json_parse_failed",
            {"input_city": city, "resolved_city": city_name},
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="天気予報APIのレスポンス解析に失敗しました",
        )

    try:
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

        log_event(
            logger,
            "info",
            "forecast_success",
            {
                "input_city": city,
                "resolved_city": city_name,
                "days": days,
                "first_date": forecast_list[0]["date"] if forecast_list else None,
            },
        )

        log_event(
            logger,
            "debug",
            "forecast_result_json",
            forecast_list,
        )

    except Exception:
        log_event(
            logger,
            "error",
            "forecast_build_failed",
            {"input_city": city, "resolved_city": city_name},
            exc_info=True,
        )
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
    log_event(
        logger,
        "info",
        "send_weather_message_started",
        {"city": city},
    )

    if not LINE_ACCESS_TOKEN:
        log_event(
            logger,
            "warning",
            "line_access_token_missing_skip_broadcast",
            {"city": city},
        )
        return

    if len(forecast) < 2:
        log_event(
            logger,
            "error",
            "forecast_insufficient_for_broadcast",
            {"city": city, "forecast_length": len(forecast)},
        )
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

    log_event(
        logger,
        "debug",
        "line_broadcast_request_json",
        body,
    )

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.post(
                "https://api.line.me/v2/bot/message/broadcast",
                headers=headers,
                json=body,
            )

            log_event(
                logger,
                "info",
                "line_broadcast_response",
                {"city": city, "status_code": res.status_code},
            )

            response_data = res.json() if res.text else {}
            log_event(
                logger,
                "debug",
                "line_broadcast_response_json",
                response_data,
            )

            res.raise_for_status()

            log_event(
                logger,
                "info",
                "send_weather_message_success",
                {"city": city},
            )

        except httpx.HTTPStatusError:
            log_event(
                logger,
                "error",
                "line_broadcast_http_status_error",
                {"city": city},
                exc_info=True,
            )
            raise
        except httpx.RequestError:
            log_event(
                logger,
                "error",
                "line_broadcast_request_error",
                {"city": city},
                exc_info=True,
            )
            raise
        except Exception:
            log_event(
                logger,
                "error",
                "line_broadcast_unexpected_error",
                {"city": city},
                exc_info=True,
            )
            raise


@app.post("/webhook")
async def webhook(request: Request):
    log_event(logger, "info", "webhook_received")

    try:
        body = await request.json()
        log_event(
            logger,
            "debug",
            "webhook_request_json",
            body,
        )
    except Exception:
        log_event(
            logger,
            "error",
            "webhook_json_parse_failed",
            exc_info=True,
        )
        raise HTTPException(status_code=400, detail="不正なJSONです")

    events = body.get("events", [])
    log_event(
        logger,
        "info",
        "webhook_events_count",
        {"count": len(events)},
    )

    for idx, event in enumerate(events):
        log_event(
            logger,
            "debug",
            "webhook_event_json",
            {"index": idx, "event": event},
        )

        if event.get("type") != "message":
            log_event(
                logger,
                "debug",
                "webhook_skip_non_message_event",
                {"index": idx, "type": event.get("type")},
            )
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            log_event(
                logger,
                "debug",
                "webhook_skip_non_text_message",
                {"index": idx, "message_type": message.get("type")},
            )
            continue

        reply_token = event.get("replyToken")
        text = message.get("text", "").strip()

        log_event(
            logger,
            "info",
            "webhook_message_processing",
            {
                "index": idx,
                "reply_token_exists": bool(reply_token),
                "text": text,
            },
        )

        try:
            await handle_message(reply_token, text)
            log_event(
                logger,
                "info",
                "webhook_message_processed",
                {"index": idx, "text": text},
            )
        except Exception:
            log_event(
                logger,
                "error",
                "webhook_message_processing_failed",
                {"index": idx, "text": text},
                exc_info=True,
            )

    return {"status": "ok"}


async def handle_message(reply_token: str, text: str):
    log_event(
        logger,
        "info",
        "handle_message_started",
        {"text": text},
    )

    city = text if text else NOTIFY_CITY

    log_event(
        logger,
        "info",
        "handle_message_resolved_city",
        {"input_text": text, "city": city},
    )

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

        log_event(
            logger,
            "info",
            "handle_message_forecast_success",
            {"input_city": city, "resolved_city": result["city"]},
        )

    except HTTPException as e:
        log_event(
            logger,
            "warning",
            "handle_message_forecast_failed",
            {
                "city": city,
                "status_code": e.status_code,
                "detail": e.detail,
            },
        )
        reply_text = (
            f"「{text}」の天気情報が見つかりませんでした。\n"
            f"都市名を送ってください。\n"
            f"例: 鎌倉、東京、Osaka"
        )
    except Exception:
        log_event(
            logger,
            "error",
            "handle_message_unexpected_error",
            {"city": city},
            exc_info=True,
        )
        reply_text = "エラーが発生しました。しばらくしてからもう一度お試しください。"

    await send_reply(reply_token, reply_text)

    log_event(
        logger,
        "info",
        "handle_message_reply_sent",
        {"text": text},
    )


async def send_reply(reply_token: str, text: str):
    log_event(
        logger,
        "info",
        "send_reply_started",
        {"reply_token_exists": bool(reply_token)},
    )

    if not LINE_ACCESS_TOKEN:
        log_event(
            logger,
            "warning",
            "line_access_token_missing_skip_reply",
        )
        return

    headers = {
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }

    log_event(
        logger,
        "debug",
        "line_reply_request_json",
        body,
    )

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.post(
                "https://api.line.me/v2/bot/message/reply",
                headers=headers,
                json=body,
            )

            log_event(
                logger,
                "info",
                "line_reply_response",
                {"status_code": res.status_code},
            )

            response_data = res.json() if res.text else {}
            log_event(
                logger,
                "debug",
                "line_reply_response_json",
                response_data,
            )

            res.raise_for_status()

            log_event(
                logger,
                "info",
                "send_reply_success",
            )

        except httpx.HTTPStatusError:
            log_event(
                logger,
                "error",
                "line_reply_http_status_error",
                exc_info=True,
            )
            raise
        except httpx.RequestError:
            log_event(
                logger,
                "error",
                "line_reply_request_error",
                exc_info=True,
            )
            raise
        except Exception:
            log_event(
                logger,
                "error",
                "line_reply_unexpected_error",
                exc_info=True,
            )
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


# 1. 定数の追加
THE_CAT_API_URL = "https://api.thecatapi.com/v1/images/search"

# --- 中略 ---

# 2. エンドポイントの追加
@app.get("/cat")
async def get_random_cat():
    """
    The Cat APIからランダムな猫画像を取得します。
    """
    log_event(logger, "info", "get_random_cat_started")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            log_event(
                logger,
                "debug",
                "the_cat_api_request",
                {"url": THE_CAT_API_URL}
            )

            # APIリクエスト
            response = await client.get(THE_CAT_API_URL)

            log_event(
                logger,
                "info",
                "the_cat_api_response",
                {"status_code": response.status_code}
            )

            response.raise_for_status()
            data = response.json()

            if not data or not isinstance(data, list):
                raise ValueError("Unexpected API response format")

            # ランダムな1件を取得
            cat_info = data[0]
            cat_id = cat_info.get("id")
            cat_url = cat_info.get("url")

            log_event(
                logger,
                "info",
                "get_random_cat_success",
                {"cat_id": cat_id, "url": cat_url}
            )

            return {
                "id": cat_id,
                "url": cat_url,
                "source": "https://thecatapi.com"
            }

        except httpx.HTTPStatusError:
            log_event(logger, "error", "the_cat_api_http_error", exc_info=True)
            raise HTTPException(status_code=502, detail="猫APIとの通信に失敗しました")
        except Exception:
            log_event(logger, "error", "the_cat_api_unexpected_error", exc_info=True)
            raise HTTPException(status_code=500, detail="予期せぬエラーが発生しました")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
        access_log=True,
    )