from fastapi import FastAPI
from .routes.health import router as health_router
from .routes.leads import build_router as leads_router
from .routes.booking import build_router as booking_router

def create_app(ctx) -> FastAPI:
    app = FastAPI(title="Eastwood", version="0.2.0")
    app.include_router(health_router)
    app.include_router(leads_router(ctx))
    app.include_router(booking_router(ctx))
    return app