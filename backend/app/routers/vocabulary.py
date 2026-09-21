# backend/app/routers/vocabulary.py
"""Vocabulary API — every label and definition a user sees, from one place.

Public, like /api/profile: the frontend reads it before the login screen so no
screen has to invent a word of its own, and the docs can quote one source.
"""

from fastapi import APIRouter

from app import vocabulary

router = APIRouter(prefix="/api/vocabulary", tags=["vocabulary"])


@router.get("")
def read_vocabulary():
    return vocabulary.as_dict()
