"""Pydantic 请求模型与领域数据结构。"""

from dataclasses import dataclass

from pydantic import BaseModel, Field


class RegisterPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class RoomPayload(BaseModel):
    name: str = Field(min_length=2, max_length=60)


class AdminLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AdminCreateUserPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)


class AdminUpdateUserPayload(BaseModel):
    password: str = Field(min_length=6, max_length=128)


@dataclass
class User:
    id: int
    username: str
