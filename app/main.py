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

# --- Constants & Configuration ---
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
THE_CAT_API_URL = "https://api.thecatapi.com/v1/images/search"

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")
NOTIFY_CITY = os.getenv("NOTIFY_CITY", "Tokyo")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HTTP_TRACE = os.getenv("HTTP_TRACE", "0") == "1"

scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

# --- Logging Setup ---
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
    if HTTP_TRACE:
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

def log_event(logger, level, message, data=None, exc_info=False):
    getattr(logger, level)(message, extra={"extra_data": data}, exc_info=exc_info)

setup_logging()
logger = logging.getLogger("minakata")

# --- Core Functions ---
async def get_random_cat():
    """The Cat APIからランダムな画像を取得"""
    log_event(logger, "info", "get_random_cat_started")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            res = await client.get(THE_CAT_API_URL)
            res.raise_for_status()
            data = res.json()
            cat_info = data[0]
            log_event(logger, "info", "get_random_cat_success", {"url": cat_info["url"]})
            return {"id": cat_info["id"], "url": cat_info["url"]}
        except Exception:
            log_event(logger, "error", "get_random_cat_failed", exc_info=True)
            raise

async def forecast(city: str, days: int = 7):
    """Open-Meteo APIを使用して天気予報を取得"""
    log_event(logger, "info", "forecast_started", {"city": city})
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Geocoding
        geo_res = await client.get(GEOCODING_URL, params={"name": city, "count": 1, "language": "ja"})
        geo_res.raise_for_status()
        geo_data = geo_res.json()
        if not geo_data.get("results"):
            raise HTTPException(status_code=404, detail="City not found")
        
        loc = geo_data["results"][0]
        # Weather
        w_params = {
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "daily": ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "weathercode"],
            "timezone": "Asia/Tokyo", "forecast_days": days
        }
        w_res = await client.get(OPEN_METEO_URL, params=w_params)
        w_res.raise_for_status()
        w_data = w_res.json()["daily"]

        forecast_list = []
        for i in range(days):
            forecast_list.append({
                "date": w_data["time"][i],
                "weather": _weather_label(w_data["weathercode"][i]),
                "temp_max": w_data["temperature_2m_max"][i],
                "temp_min": w_data["temperature_2m_min"][i],
                "precipitation_mm": w_data["precipitation_sum"][i],
            })
        return {"city": loc["name"], "forecast": forecast_list}

async def send_daily_forecast():
    try:
        res = await forecast(city=NOTIFY_CITY)
        today, tomorrow = res["forecast"][0], res["forecast"][1]
        text = f"【定期】{res['city']}の天気\n今日: {today['weather']} ({today['temp_max']}℃)\n明日: {tomorrow['weather']} ({tomorrow['temp_max']}℃)"
        await send_broadcast([{"type": "text", "text": text}])
    except Exception:
        log_event(logger, "error", "scheduled_job_failed", exc_info=True)

# --- LINE Messaging Helpers ---
async def send_reply(reply_token: str, messages: list):
    """LINEにリプライを送信 (messagesはdictのリスト)"""
    if not LINE_ACCESS_TOKEN or not reply_token:
        return
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {"replyToken": reply_token, "messages": messages}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post("https://api.line.me/v2/bot/message/reply", headers=headers, json=body)
        res.raise_for_status()

async def send_broadcast(messages: list):
    """LINEにブロードキャスト送信"""
    if not LINE_ACCESS_TOKEN: return
    headers = {"Authorization": f"Bearer {LINE_ACCESS_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post("https://api.line.me/v2/bot/message/broadcast", headers=headers, json={"messages": messages})
        res.raise_for_status()

def _weather_label(code: int) -> str:
    mapping = {0: "快晴", 1: "晴れ", 2: "時々曇り", 3: "曇り", 45: "霧", 48: "霧", 61: "小雨", 63: "雨", 80: "にわか雨"}
    return mapping.get(code, "その他")

# --- FastAPI App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(send_daily_forecast, "cron", hour=8, minute=0, id="daily_job")
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="minakata", version="0.2.0", lifespan=lifespan)

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/cat")
async def cat_endpoint():
    return await get_random_cat()

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    events = body.get("events", [])
    for event in events:
        if event.get("type") == "message" and event["message"].get("type") == "text":
            reply_token = event.get("replyToken")
            text = event["message"].get("text", "").strip()
            await handle_message(reply_token, text)
    return {"status": "ok"}

async def handle_message(reply_token: str, text: str):
    # 猫モード
    if "猫" in text or "cat" in text.lower():
        try:
            cat = await get_random_cat()
            messages = [{
                "type": "image",
                "originalContentUrl": cat["url"],
                "previewImageUrl": cat["url"]
            }]
            await send_reply(reply_token, messages)
            return
        except Exception:
            await send_reply(reply_token, [{"type": "text", "text": "猫ちゃんが迷子になりました。"}])
            return

    # 天気予報モード
    try:
        res = await forecast(city=text if text else NOTIFY_CITY)
        f = res["forecast"]
        msg = f"🌤 {res['city']}の予報\n今日: {f[0]['weather']} {f[0]['temp_max']}℃\n明日: {f[1]['weather']} {f[1]['temp_max']}℃"
        await send_reply(reply_token, [{"type": "text", "text": msg}])
    except Exception:
        await send_reply(reply_token, [{"type": "text", "text": "都市名が見つかりませんでした。"}])

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)