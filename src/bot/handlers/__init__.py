from .admin import expert_admin_router, professor_admin_router, dose_admin_router
from .user import expert_user_router, dose_user_router
from .new_user import professor_user_router
from .new_guest import professor_guest_router
from .new_chat import professor_chat_router
from . import new_admin as _professor_admin_handlers  # noqa: F401 - register extra professor admin handlers

__all__ = [
    "expert_user_router",
    "expert_admin_router",
    "professor_user_router",
    "professor_admin_router",
    "professor_guest_router",
    "dose_user_router",
    "dose_admin_router",
    "professor_chat_router",
]
