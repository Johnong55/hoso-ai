# app/api/v1/router.py
from fastapi import APIRouter

from app.api.v1.endpoints import auth, chat, feedback, forms, procedures
from app.api.v1.endpoints.admin import sources, stats, users

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(procedures.router)
api_router.include_router(feedback.router)
api_router.include_router(forms.router)

# Admin sub-routes
admin_router = APIRouter(prefix="/admin")
admin_router.include_router(sources.router)
admin_router.include_router(stats.router)
admin_router.include_router(users.router)

api_router.include_router(admin_router)
