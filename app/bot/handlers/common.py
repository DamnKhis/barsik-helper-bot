from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards.reply import menu_for_role

router = Router(name="common")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, role: str) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=menu_for_role(role))


@router.message(Command("help"))
async def help_command(message: Message, role: str) -> None:
    if role == "admin":
        text = "Админ-панель: пришлите документ, чтобы добавить его в базу знаний."
    else:
        text = "Нажмите «Задать вопрос», чтобы поговорить с ботом. /cancel — выйти из диалога."
    await message.answer(text, reply_markup=menu_for_role(role))


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(F.text == "")
async def empty_message(message: Message, role: str) -> None:
    await message.answer("Похоже, сообщение пустое.", reply_markup=menu_for_role(role))
