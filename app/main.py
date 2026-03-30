from fastapi import FastAPI
from datetime import datetime

app = FastAPI(title="minakata", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "Ça va bien", "timestamp": datetime.utcnow().isoformat()}


@app.get("/")
def root():
    return {"message": "minakata API"}
