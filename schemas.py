from typing import Literal, Optional
from aiohttp import web
from pydantic import BaseModel, Field, ValidationError, field_validator


VALID_TX_TYPES = ("expense", "income", "transfer")
VALID_ASSET_CLASSES = ("stock", "crypto", "bond", "deposit")


def _clean_currency(v: str) -> str:
    c = (v or "UAH").strip().upper()
    if not c or len(c) > 10 or not c.isalnum():
        raise ValueError("Некоректний код валюти")
    return c


class TransactionCreate(BaseModel):
    account_id: str = Field(..., min_length=1)
    to_account_id: Optional[str] = None
    category_id: Optional[str] = None
    amount: float = Field(..., gt=0)
    target_amount: Optional[float] = Field(default=None, gt=0)
    type: Literal["expense", "income", "transfer"] = "expense"
    note: str = Field(default="", max_length=500)
    transaction_date: Optional[str] = None


class TransactionUpdate(BaseModel):
    account_id: Optional[str] = None
    to_account_id: Optional[str] = None
    category_id: Optional[str] = None
    amount: float = Field(..., gt=0)
    target_amount: Optional[float] = Field(default=None, gt=0)
    type: Literal["expense", "income", "transfer"]
    note: str = Field(default="", max_length=500)
    transaction_date: Optional[str] = None


class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    account_type: str = Field(default="card", max_length=40)
    currency: str = Field(default="UAH")
    balance: float = 0.0

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class AccountUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    account_type: str = Field(default="card", max_length=40)
    currency: str = Field(default="UAH")
    balance: float = 0.0

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    target_amount: float = Field(..., gt=0)
    current_amount: float = Field(default=0.0, ge=0)
    currency: str = Field(default="UAH")
    deadline: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class GoalUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    target_amount: float = Field(..., gt=0)
    currency: str = Field(default="UAH")
    deadline: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class GoalFundsModify(BaseModel):
    goal_id: str = Field(..., min_length=1)
    account_id: str = Field(..., min_length=1)
    amount: float = Field(..., gt=0)
    action: Literal["deposit", "withdraw"]


class InvestmentCreate(BaseModel):
    name: Optional[str] = Field(default="", max_length=150)
    ticker: Optional[str] = Field(default="", max_length=30)
    asset_class: Literal["stock", "crypto", "bond", "deposit"] = "stock"
    quantity: float = Field(..., gt=0)
    buy_price_avg: float = Field(..., gt=0)
    current_price: Optional[float] = Field(default=None, ge=0)
    currency: str = Field(default="USD")
    interest_rate: float = Field(default=0.0, ge=0)
    maturity_date: Optional[str] = None
    term_months: Optional[int] = Field(default=None, ge=0)
    account_id: Optional[str] = None  # optional account debit on purchase

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class InvestmentUpdate(BaseModel):
    name: Optional[str] = Field(default="", max_length=150)
    ticker: Optional[str] = Field(default="", max_length=30)
    asset_class: Literal["stock", "crypto", "bond", "deposit"] = "stock"
    quantity: float = Field(..., gt=0)
    buy_price_avg: float = Field(..., gt=0)
    current_price: float = Field(..., ge=0)
    currency: str = Field(default="USD")
    interest_rate: float = Field(default=0.0, ge=0)
    maturity_date: Optional[str] = None
    term_months: Optional[int] = Field(default=None, ge=0)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    type: Literal["expense", "income"] = "expense"
    icon: str = Field(default="🏷️", max_length=16)


class BudgetCreate(BaseModel):
    category_id: str = Field(..., min_length=1)
    monthly_limit: float = Field(..., gt=0)
    currency: str = Field(default="UAH")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _clean_currency(v)


class RecurringCreate(BaseModel):
    account_id: str = Field(..., min_length=1)
    category_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=150)
    amount: float = Field(..., gt=0)
    type: Literal["expense", "income"] = "expense"
    day_of_month: int = Field(default=1, ge=1, le=31)


class PriceAlertCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    asset_class: Literal["stock", "crypto"] = "crypto"
    condition: Literal["above", "below"] = "above"
    target_price: float = Field(..., gt=0)


class UserSettingsUpdate(BaseModel):
    default_account_id: Optional[str] = None
    base_currency: Optional[str] = None
    weekly_digest: Optional[bool] = None
    monthly_digest: Optional[bool] = None
    budget_alerts_enabled: Optional[bool] = None
    daily_reminder: Optional[bool] = None
    notifications_enabled: Optional[bool] = None

    @field_validator("base_currency")
    @classmethod
    def check_base_currency(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _clean_currency(v)


class CsvImportItem(BaseModel):
    date: Optional[str] = None
    amount: float
    type: Literal["expense", "income"] = "expense"
    note: str = Field(default="", max_length=500)
    category_id: Optional[str] = None


class CsvImportRequest(BaseModel):
    account_id: str = Field(..., min_length=1)
    items: list[CsvImportItem] = Field(..., min_length=1, max_length=500)


async def parse_json_body(request: web.Request, model_cls: type[BaseModel]):
    try:
        raw = await request.json()
    except Exception:
        return None, web.json_response({"error": "Некоректний JSON у тілі запиту"}, status=400)
    try:
        return model_cls.model_validate(raw), None
    except ValidationError as exc:
        msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
        return None, web.json_response({"error": f"Помилка валідації: {msg}"}, status=400)
