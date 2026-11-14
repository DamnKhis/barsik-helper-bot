from __future__ import annotations

from pathlib import Path

from docx import Document


def make_fixtures(settings) -> None:
    out = Path(settings.data_dir) / "fixtures"
    out.mkdir(parents=True, exist_ok=True)

    core = Document()
    core.add_heading("Тестовый регламент БАРС", level=0)
    core.add_heading("1. Назначение", level=1)
    core.add_paragraph(
        "Документ проверяет парсер DOCX: заголовки, списки, таблицы, гиперссылки и удаление мусорного футера."
    )
    core.add_heading("1.1. Порядок действий", level=2)
    core.add_paragraph("1. Открыть БАРС-Офис.")
    core.add_paragraph("2. Перейти в раздел «Документы».")
    core.add_paragraph("3. Нажать кнопку «Создать».")
    core.add_heading("1.2. Таблица параметров", level=2)
    table = core.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    headers = table.rows[0].cells
    headers[0].text = "Параметр"
    headers[1].text = "Значение"
    headers[2].text = "Примечание"
    for name, value, note in [
        ("Подразделение", "Бизнес-центр", "Обязательно"),
        ("Номер пресейла", "2334-26", "Статус воронки >= 60%"),
        ("Командировочные", "10000", "Иначе БП не запустится"),
    ]:
        row = table.add_row().cells
        row[0].text = name
        row[1].text = value
        row[2].text = note
    core.add_paragraph("Ошибка! Используйте вкладку \"Главная\" для применения Heading 1 к тексту, который")
    core.add_paragraph("должен здесь отображаться. – 0")
    core.save(out / "fixture_core.docx")

    tricky = Document()
    tricky.add_heading("Фильтры и ссылки", level=1)
    tricky.add_paragraph("В фильтрах поддерживаются операторы >, <, =, !=. Оператор !> не поддерживается.")
    tricky.add_paragraph(
        "Пример ссылки: https://bo.bars.group//#mainmenuitemclick&bars_office&byudjeetsdelki&id&=&6DF3B1B8B2F249368E5F9749C10CD2AC"
    )
    tricky.add_paragraph("Сделка 2334-26 со статусом воронки 30% не попадёт в список, если для БЦ требуется >= 60%.")
    tricky.add_paragraph("В документации описан реестр мебели, а не реестр техники.")
    tricky.save(out / "fixture_tricky.docx")

    for version, prefix in (("v1", "Первая версия раздела."), ("v2", "Вторая версия раздела с изменённым текстом.")):
        doc = Document()
        doc.add_heading("Обновляемый документ", level=0)
        doc.add_heading("1. Раздел", level=1)
        doc.add_paragraph(prefix)
        doc.add_paragraph("Общая часть, которая не меняется.")
        doc.save(out / f"fixture_update_{version}.docx")
