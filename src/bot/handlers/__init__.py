from .admin import professor_admin_router, dose_admin_router, new_admin_router
from .user import professor_user_router, dose_user_router
from .new_user import new_user_router
from .new_guest import new_guest_router
from .new_chat import new_chat_router
from .new_admin import new_admin_router

__all__ = [
    "professor_user_router",
    "professor_admin_router",
    "new_user_router",
    "new_guest_router",
    "dose_user_router",
    "dose_admin_router",
    "new_admin_router",
    "new_chat_router",
]
