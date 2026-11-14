import asyncio
from app.rag.retriever import Retriever

BENCHMARK_12_QUESTIONS = [
    # 1-10: Ожидаем флаг ANSWER и вес >= 0.70
    (1, "Как выглядит интерфейс БО? Где найти в нем производственный календарь?", "ANSWER"),
    (2, "Как работает фильтр документа? Опиши шаг действий для настройки фильтра", "ANSWER"),
    (3, "Почему не работает фильтр в БО \"!>200\"?", "ANSWER"),
    (4, "Мне прислали ссылку https://bo.bars.group//#mainmenuitemclick&bars_office&byudjeetsdelki&id&=&6DF3B1B8B2F249368E5F9749C10CD2AC что с ней делать?", "ANSWER"),
    (5, "Мне необходимо узнать, кто согласовывает БП Командировки?", "ANSWER"),
    (6, "Мне не отвечает офис-менеджер, какой у него график работы?", "ANSWER"),
    (7, "Почему не могу выбрать сделку 2334-26, у которой статус воронки 30% в поле Номер пресейла?", "ANSWER"),
    (8, "Смогу ли я запустить БП Командировка, если не заложил средств в бюджет сделки?", "ANSWER"),
    (9, "Какие могут быть условия в выборе следующего согласующего лица БП Командировка?", "ANSWER"),
    (10, "Какие функциональные кнопки есть в реестре мебели? Как они выглядят?", "ANSWER"),
    # 11-12: Вопросы-ловушки про несуществующий «Реестр техники» -> ожидаем ESCALATE + ответ по Реестру мебели
    (11, "Опиши поэтапно как в БО добавить запись в реестр техники?", "ESCALATE"),
    (12, "Какие статусы мебели перечислены в реестре техники?", "ESCALATE"),
]

EXTRA_CHECKS = [
    ("ОФФТОП", "Скажи, синее ли небо сегодня?", "OFF_TOPIC"),
    ("СЛОЖНЫЙ ВОПРОС", "В БАРС-Офисе при синхронизации сертификата КриптоПро возникает ошибка 0x80090016, что делать?", "ESCALATE"),
    ("ДУБЛЬ ДЛЯ ТОП-10", "Подскажите часы работы и телефон дежурного офис-менеджера", "ANSWER"),
]


async def main():
    retriever = Retriever()
    passed = 0

    print("\n================ ПРОВЕРКА 12 ТЕСТ-КЕЙСОВ ЖЮРИ ================")
    for num, query, expected_flag in BENCHMARK_12_QUESTIONS:
        res = await retriever.answer_query(query)
        is_ok = (res.flag == expected_flag) and (res.confidence >= 0.70 if expected_flag == "ANSWER" else True)
        status = "✅ OK" if is_ok else "⚠️ ПРОВЕРИТЬ"
        if is_ok:
            passed += 1

        print(f"\n[Кейс #{num}] {status}")
        print(f"Вопрос: {query}")
        print(f"Флаг: {res.flag} (ожидался {expected_flag}) | Вес: {res.confidence} | Оператор: {res.need_operator}")
        print(f"Источник: {res.source}")
        if res.images:
            print(f"Скриншоты: {res.images}")
        print(f"Ответ: {res.answer[:180]}...")

    print(f"\nИТОГ ПО БРИФУ: Успешно пройдено {passed} из 12 тест-кейсов.")

    print("\n================ ДОПОЛНИТЕЛЬНЫЕ ПРОВЕРКИ РОУТЕРА ================")
    for label, query, expected_flag in EXTRA_CHECKS:
        res = await retriever.answer_query(query)
        status = "✅ OK" if res.flag == expected_flag else "⚠️ ПРОВЕРИТЬ"
        print(f"[{label}] {status} | Флаг: {res.flag} | Вес: {res.confidence} | Оператор: {res.need_operator}")

    print("\n================ ТОП-10 ЧАСТЫХ ВОПРОСОВ В QDRANT ================")
    top_10 = await retriever.get_top_10_questions()
    for i, item in enumerate(top_10, 1):
        print(f"{i}. «{item['question']}» — спросили раз: {item['count']}")


if __name__ == "__main__":
    asyncio.run(main())
