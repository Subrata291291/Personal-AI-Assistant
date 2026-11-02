
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.session import get_db
from app.schemas.memory import (
    MemoryCreate,
    MemoryUpdate,
    MemoryResponse,
)
from app.services.memory_service import MemoryService


router = APIRouter()


def get_memory_service(
    db: Annotated[Session, Depends(get_db)],
) -> MemoryService:
    return MemoryService(db)


MemoryServiceDependency = Annotated[
    MemoryService,
    Depends(get_memory_service),
]


@router.post(
    "",
    response_model=MemoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_memory(
    payload: MemoryCreate,
    user: CurrentUser,
    service: MemoryServiceDependency,
):
    try:
        return service.create_memory(
            user_id=UUID(str(user["id"])),
            memory_type=payload.memory_type,
            content=payload.content,
            importance=payload.importance,
            confidence=payload.confidence,
            metadata=payload.metadata,
            source_conversation_id=payload.source_conversation_id,
            expires_at=payload.expires_at,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=list[MemoryResponse],
)
def list_memories(
    user: CurrentUser,
    service: MemoryServiceDependency,
    memory_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    try:
        return service.list_memories(
            user_id=UUID(str(user["id"])),
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.get(
    "/{memory_id}",
    response_model=MemoryResponse,
)
def get_memory(
    memory_id: UUID,
    user: CurrentUser,
    service: MemoryServiceDependency,
):
    memory = service.get_memory(
        user_id=UUID(str(user["id"])),
        memory_id=memory_id,
    )

    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found.",
        )

    return memory


@router.patch(
    "/{memory_id}",
    response_model=MemoryResponse,
)
def update_memory(
    memory_id: UUID,
    payload: MemoryUpdate,
    user: CurrentUser,
    service: MemoryServiceDependency,
):
    try:
        memory = service.update_memory(
            user_id=UUID(str(user["id"])),
            memory_id=memory_id,
            content=payload.content,
            importance=payload.importance,
            confidence=payload.confidence,
            metadata=payload.metadata,
            expires_at=payload.expires_at,
        )

        if memory is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Memory not found.",
            )

        return memory

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_memory(
    memory_id: UUID,
    user: CurrentUser,
    service: MemoryServiceDependency,
):
    deleted = service.delete_memory(
        user_id=UUID(str(user["id"])),
        memory_id=memory_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory not found.",
        )
