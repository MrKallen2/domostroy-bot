from aiogram.fsm.state import State, StatesGroup


class OrderStates(StatesGroup):
    # Этап 0-1: Параметры дома
    choosing_service = State()  # с монтажом / без
    entering_dimensions = State()  # габариты дома
    choosing_material = State()  # материал
    choosing_floors = State()  # этажность
    choosing_pile_count = State()  # Добавить это

    # Этап 2: Выбор свай
    showing_recommendations = State()  # показ рекомендаций
    choosing_piles = State()  # самостоятельный выбор / подтверждение

    # Этап 3: Самовывоз/доставка
    choosing_pickup_delivery = State()  # самовывоз или доставка
    entering_district = State()  # район (для доставки)
    entering_address_pickup = State()  # адрес для самовывоза

    # Этап 4: Дата и время
    choosing_date = State()  # выбор даты из свободных
    entering_time = State()  # ввод удобного времени

    # Этап 5: Контакты
    entering_fio = State()
    entering_phone = State()
    entering_address = State()  # точный адрес объекта

    # Этап 6: Техника
    checking_big_equipment = State()  # заедет ли большая?
    checking_small_equipment = State()  # заедет ли маленькая?
    checking_manual = State()  # проверка условий ручного монтажа

    # Этап 7: Электричество
    checking_electricity = State()
    generator_days_input = State()

    # Этап 8: Геология
    checking_geology = State()
    offering_guarantee = State()  # доп гарантия если геология есть

    # Этап 9-10: Смета и изменения
    showing_estimate = State()  # показ итоговой сметы
    changing_estimate = State()  # изменение сметы

    # Этап 11: Оплата
    waiting_payment = State()  # ожидание подтверждения оплаты

    # Финальные статусы
    completed = State()  # заказ завершён

    waiting_for_ready = State()  # ожидание подтверждения готовности

    # Самостоятельный выбор свай
    choosing_pile_diameter = State()  # выбор диаметра
    choosing_pile_length = State()    # выбор длины

    pile_confirmation = State()  # подтверждение выбора сваи

    # Отложенные уведомления
    day_before_confirmation = State()  # подтверждение за день
    reschedule_date = State()  # перенос даты
    morning_check = State()  # проверка утром

    data_confirmation = State()  # проверка данных перед сметой