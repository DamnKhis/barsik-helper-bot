from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class RoleFilter(BaseFilter):
    """Пропускает апдейт, только если роль из БД совпадает с ожидаемой."""

    def __init__(self, role: str) -> None:
        self.role = role

    async def __call__(self, event: TelegramObject, **kwargs) -> bool:
        return kwargs.get("role") == self.role
