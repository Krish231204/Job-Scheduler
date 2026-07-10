import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.errors import register_error_handlers
from app.routers import auth, dashboard, jobs, orgs, projects, queues, workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

app = FastAPI(
    title="Job Scheduler",
    description="A distributed job scheduling platform: queues, workers, retries, and a live dashboard.",
    version="1.0.0",
)

register_error_handlers(app)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(projects.router)
app.include_router(queues.router)
app.include_router(jobs.router)
app.include_router(workers.router)
app.include_router(dashboard.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
