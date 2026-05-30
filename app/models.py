"""Pydantic-схеми запитів/відповідей API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RegisterJarRequest(BaseModel):
    bank: Optional[str] = Field(default=None, description="Банк/адаптер; якщо не вказано — визначається з url")
    url: str = Field(description="Посилання на банку/збір або готовий ref")
    card: Optional[str] = Field(default=None, description="Номер картки (лейбл/звірка)")
    callback_url: Optional[str] = Field(default=None, description="URL для колбеків про зарахування")


class ResolveRefRequest(BaseModel):
    url: str = Field(description="Посилання на банку/збір або готовий ref")
    bank: Optional[str] = Field(default=None, description="Банк/адаптер; якщо не вказано — визначається з url")


class ResolveRefOut(BaseModel):
    ref: str
    bank: str


class JarOut(BaseModel):
    ref: str
    bank: str
    url: Optional[str]
    card: Optional[str]
    name: Optional[str]
    currency: str
    baseline_amount: int
    last_amount: int
    last_withdrawal: int
    balance: int          # поточний баланс банки (last_amount - last_withdrawal); падає при знятті
    accumulated: int      # усього накопичено з моменту підписки (сума поповнень); монотонне
    callback_url: Optional[str]
    status: str
    created_at: int
    last_polled_at: Optional[int]
    last_error: Optional[str]


class CreditOut(BaseModel):
    id: str
    jar_ref: str
    bank: str
    card: Optional[str]
    amount: int
    balance_after: int
    currency: str
    detected_at: int
    callback_status: str
    callback_attempts: int
    callback_last_at: Optional[int]
    callback_last_error: Optional[str]
