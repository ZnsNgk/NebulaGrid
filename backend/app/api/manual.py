from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.responses import api_success
from app.services.auth_service import UserRecord
from app.services.manual_service import get_manual_document

router = APIRouter()


@router.get("/current")
def get_current_manual(request: Request, current_user: UserRecord = Depends(get_current_user)):
    """返回当前用户角色对应的使用手册 Markdown。"""
    document = get_manual_document(current_user)
    return api_success(data=document.model_dump(), request_id=request.headers.get("x-request-id"))
