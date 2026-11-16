from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.config import APP_VERSION
from app.errors import DomainError
from app.store import init_db
from app.modules.m1_legs.handler import router as m1_legs_router
from app.modules.m2_airports.handler import router as m2_airports_router
from app.modules.m3_localtime.handler import router as m3_localtime_router
from app.modules.m4_itinerary.handler import router as m4_itinerary_router
from app.modules.m5_connections.handler import router as m5_connections_router
from app.modules.m6_batches.handler import router as m6_batches_router
from app.modules.m7_mct.handler import router as m7_mct_router
from app.modules.m7_mct.store import init_db as init_mct_db

app = FastAPI(title="航班时刻运行台", version=APP_VERSION)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.state.templates = Jinja2Templates(directory="app/templates")
app.state.version = APP_VERSION
app.include_router(m1_legs_router)
app.include_router(m2_airports_router)
app.include_router(m3_localtime_router)
app.include_router(m4_itinerary_router)
app.include_router(m5_connections_router)
app.include_router(m6_batches_router)
app.include_router(m7_mct_router)


@app.exception_handler(DomainError)
def domain_error_handler(request, exc: DomainError):
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message}})


@app.on_event("startup")
def startup():
    init_db()
    init_mct_db()

@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/legs")

@app.get("/api/health")
def health():
    return {"status": "ok", "version": APP_VERSION}

