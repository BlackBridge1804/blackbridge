import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .auth import hash_password
from .config import settings
from .database import Base, SessionLocal, engine
from .models import PartnerOffer, User, UserRole
from .routers import analytics, assistant, auth, billing, letters, offers, organizations, recommendations, reports, staff

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="BlackBridge (Credit Repair AI Platform)",
    description=(
        "Reference implementation of the architecture in the strategy doc: "
        "multi-tenant auth, report upload/parsing, a deterministic FCRA/Metro 2 "
        "rules engine, and a paywalled dispute-letter generator. Not legal "
        "advice, not production-hardened -- see README.md before using with "
        "real consumer data."
    ),
)

# Lets the same static frontend be served from a different origin during local
# development (e.g. opening the HTML file directly, or a separate dev server)
# without CORS errors. In production the frontend is served from this same
# FastAPI app (see below), so same-origin requests don't even need this --
# it's here as a safety net, not a requirement.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(reports.router)
app.include_router(recommendations.router)
app.include_router(letters.router)
app.include_router(billing.router)
app.include_router(analytics.router)
app.include_router(staff.router)
app.include_router(assistant.router)
app.include_router(offers.router)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# The whole frontend is one static, self-contained file (app/static/index.html)
# that talks to the API above with plain fetch() calls -- no build step, no
# separate frontend server, works on mobile and desktop from the same URL.
# Mounted at /static rather than "/" so it can't shadow the API routes above.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.on_event("startup")
def bootstrap_platform_admin():
    """Creates the first platform-admin account from .env on first run, so
    there's a way into /organizations without hand-editing the database."""
    db: Session = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == settings.platform_admin_email).first()
        if not existing:
            admin = User(
                email=settings.platform_admin_email,
                hashed_password=hash_password(settings.platform_admin_password),
                role=UserRole.platform_admin,
                organization_id=None,
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}
