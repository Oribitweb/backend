from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import Optional
from math import ceil

from app.api.deps import get_db
from app.schemas.category import (
    CategoryCreate, CategoryUpdate, CategoryResponse, 
    CategoryListResponse
)
from app.crud.category import category_crud
from app.services.auth_service import get_current_user_from_cookie
from app.models.user import User  # Assuming you have User model

router = APIRouter(prefix="/categories", tags=["Categories"])

# Public endpoints (no authentication required)
@router.post("/", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie)  # Only authenticated users can create
):
    """
    Create a new category (Authentication required)
    """
    # Check if category with same name exists
    if await category_crud.exists_by_name(db, category_in.name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this name already exists"
        )
    
    # Check if slug exists (if provided, else it will be auto-generated)
    if category_in.slug and await category_crud.exists_by_slug(db, category_in.slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug already exists"
        )
    
    category = await category_crud.create(db, category_in)
    return category

@router.get("/", response_model=CategoryListResponse)
async def list_categories(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(20, ge=1, le=100, description="Number of records to return"),
    search: Optional[str] = Query(None, description="Search by name or description"),
    db: Session = Depends(get_db)
):
    """
    Get list of categories with pagination and search
    """
    categories, total = await category_crud.get_multi(db, skip=skip, limit=limit, search=search)
    
    pages = ceil(total / limit) if limit > 0 else 0
    
    return CategoryListResponse(
        items=categories,
        total=total,
        page=skip // limit + 1 if limit > 0 else 1,
        size=limit,
        pages=pages
    )

@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: int,
    db: Session = Depends(get_db)
):
    """
    Get a specific category by ID
    """
    category = category_crud.get(db, category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return category

@router.get("/slug/{slug}", response_model=CategoryResponse)
async def get_category_by_slug(
    slug: str,
    db: Session = Depends(get_db)
):
    """
    Get a specific category by slug
    """
    category = category_crud.get_by_slug(db, slug)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return category

@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    category_in: CategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie)  # Authentication required
):
    """
    Update a category (Authentication required)
    """
    # Check if category exists
    if not category_crud.exists(db, category_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    # Check name uniqueness if updating
    if category_in.name and category_crud.exists_by_name(db, category_in.name, exclude_id=category_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this name already exists"
        )
    
    # Check slug uniqueness if updating
    if category_in.slug and category_crud.exists_by_slug(db, category_in.slug, exclude_id=category_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug already exists"
        )
    
    category = category_crud.update(db, category_id, category_in)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    return category

@router.patch("/{category_id}", response_model=CategoryResponse)
async def partial_update_category(
    category_id: int,
    category_in: CategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie)  # Authentication required
):
    """
    Partial update a category (Authentication required)
    """
    return await update_category(category_id, category_in, db, current_user)

@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie)  # Authentication required
):
    """
    Delete a category (Authentication required)
    """
    deleted = category_crud.delete(db, category_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return None

@router.head("/{category_id}", status_code=status.HTTP_200_OK)
async def check_category_exists(
    category_id: int,
    db: Session = Depends(get_db)
):
    """
    Check if category exists (HEAD request)
    """
    if not category_crud.exists(db, category_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return None