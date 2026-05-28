import gspread
import time
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_FILE
import logging
from datetime import datetime  # ✅ добавил импорт
from utils import get_material_props


class GoogleSheets:
    def __init__(self):
        # Настройка доступа
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]

        try:
            # Загружаем учетные данные из JSON-файла
            self.creds = Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE, scopes=scope
            )
            self.client = gspread.authorize(self.creds)

            # Пока не открываем конкретную таблицу - нужно будет вызвать set_spreadsheet
            self.spreadsheet = None
            self.price_sheet = None
            self.delivery_sheet = None
            self.mounting_sheet = None
            self.time_sheet = None
            self.schedule_sheet = None
            self.calc_spreadsheet = None  # для расчётной таблицы

            logging.info("✅ Google Sheets client initialized")
        except Exception as e:
            logging.error(f"❌ Google Sheets init error: {e}")
            raise

    def save_to_source_sheet(self, user_data, max_retries=3):
        """Записывает данные в расчетную таблицу"""
        print(f"🔥 save_to_source_sheet вызвана с user_data: {user_data}")

        if not self.calc_spreadsheet:
            print("❌ Ошибка: calc_spreadsheet не подключен!")
            return False

        for attempt in range(max_retries):
            try:
                print(f"🔥 Попытка {attempt + 1} записи в таблицу...")
                source_sheet = self.calc_spreadsheet.worksheet("исходные данные")
                print(f"🔥 Лист 'исходные данные' найден")

                from utils import get_material_props
                width, weight = get_material_props(user_data['material'])
                print(f"🔥 Материал: {user_data['material']}, ширина={width}, вес={weight}")

                val_l = int(user_data['length'] * 1000)
                val_w = int(user_data['width'] * 1000)
                floors = int(user_data.get('floors', 1))
                print(f"🔥 Длина={val_l}мм, ширина={val_w}мм, этажей={floors}")

                # Пробуем записать по одной ячейке для теста
                print(f"🔥 Записываю B6...")
                source_sheet.update('B6', [[val_l]])
                print(f"🔥 Записываю B7...")
                source_sheet.update('B7', [[val_w]])
                print(f"🔥 Записываю B8...")
                source_sheet.update('B8', [[user_data['material']]])
                print(f"🔥 Записываю B9...")
                source_sheet.update('B9', [[width]])
                print(f"🔥 Записываю B10...")
                source_sheet.update('B10', [[weight]])

                print(f"✅ Данные успешно записаны в расчетную таблицу")
                return True

            except Exception as e:
                print(f"❌ Ошибка записи (попытка {attempt + 1}): {e}")
                import traceback
                traceback.print_exc()
                if "429" in str(e):
                    time.sleep(2)
                else:
                    return False
        return False

    def clear_source_sheet(self):
        """Очищает ячейки исходных данных после расчёта"""
        if not self.calc_spreadsheet:
            return False

        try:
            source_sheet = self.calc_spreadsheet.worksheet("исходные данные")

            print("🧹 Очищаем ячейки исходных данных...")

            # Ячейки 1 этажа
            source_sheet.update('B6', [['']])
            source_sheet.update('B7', [['']])
            source_sheet.update('B8', [['']])
            source_sheet.update('B9', [['']])
            source_sheet.update('B10', [['']])

            # Ячейки 2 этажа
            source_sheet.update('B12', [['']])
            source_sheet.update('B13', [['']])
            source_sheet.update('B17', [['']])

            # Ячейки 3 этажа
            source_sheet.update('B20', [['']])
            source_sheet.update('B21', [['']])
            source_sheet.update('B25', [['']])

            print("🧹 Ячейки исходных данных очищены")
            return True
        except Exception as e:
            print(f"❌ Ошибка очистки: {e}")
            return False

    def set_calc_spreadsheet(self, spreadsheet_url_or_id):
        """Подключается к инженерному калькулятору"""
        try:
            self.calc_spreadsheet = self.client.open_by_url(spreadsheet_url_or_id)
            print(f"✅ Подключено к расчётной таблице")
            return True
        except Exception as e:
            print(f"❌ Ошибка подключения к расчётной таблице: {e}")
            return False

    def get_pile_result(self, length, width, step=None, floors=1):
        """Забирает общий вес дома и подбирает сваи по сетке"""
        if not self.calc_spreadsheet:
            print("❌ Нет подключения к расчётной таблице")
            return None

        try:
            result_sheet = self.calc_spreadsheet.worksheet("ИТОГ")
            all_rows = result_sheet.get_all_values()

            if len(all_rows) < 2:
                return None

            # ===== 1. Ищем ОБЩИЙ ВЕС ДОМА =====
            total_weight = 0
            found_weight = False

            for i, row in enumerate(all_rows):
                row_text = ' '.join(str(cell) for cell in row)
                if "Общий вес дома" in row_text:
                    if len(row) > 1:
                        val = str(row[1]).strip().replace(',', '.')
                        try:
                            total_weight = float(val)
                            found_weight = True
                            break
                        except ValueError:
                            pass

            if not found_weight:
                return None

            # ===== 2. ОПРЕДЕЛЯЕМ ШАГ =====
            import math

            if step is None:
                max_side = max(length, width)
                if max_side < 8:
                    step = 2.0
                elif max_side <= 10:
                    step = 2.5
                else:
                    step = 3.0

            # ===== 3. РАСЧЕТ КОЛИЧЕСТВА СВАЙ (КАК НА САЙТЕ) =====
            # Отступ от края 0.5м (как на сайте)
            edge_offset = 0.5

            # Эффективная длина/ширина (расстояние между крайними сваями)
            effective_length = length - (edge_offset * 2)
            effective_width = width - (edge_offset * 2)

            # Количество промежутков между сваями
            gaps_col = max(1, math.ceil(effective_length / step))
            gaps_row = max(1, math.ceil(effective_width / step))

            # Количество свай = промежутки + 1
            cols = gaps_col + 1
            rows = gaps_row + 1

            # Общее количество свай
            count_by_grid = cols * rows

            print(f"📏 Размеры: {length}×{width}м, шаг {step}м")
            print(f"📊 Свай по длине: {cols}, по ширине: {rows}")
            print(f"📌 Итого: {count_by_grid} свай")

            # ===== 4. Таблица несущей способности =====
            pile_capacity = {
                57: 1.5,
                76: 2.21,
                89: 3.12,
                108: 4.25,
                133: 5.6,
                159: 10.2
            }

            # ===== 5. Подбираем диаметр =====
            selected_diameter = 108
            required_capacity = total_weight / count_by_grid

            print(f"⚖️ Требуется несущая способность на сваю: {required_capacity:.2f} т")

            sorted_diameters = sorted(pile_capacity.keys())

            for diam in sorted_diameters:
                capacity = pile_capacity[diam]
                if capacity >= required_capacity:
                    selected_diameter = diam
                    print(f"✅ Подходит диаметр {diam}мм (несёт {capacity}т)")
                    break

            # ===== 6. Минимальный диаметр для построек =====
            if selected_diameter < 89:
                print(f"⚠️ Диаметр {selected_diameter} слишком маленький, увеличиваем до 89")
                selected_diameter = 89

            # ===== 7. Проверяем минимальное необходимое количество по нагрузке =====
            min_needed = math.ceil(total_weight / pile_capacity[selected_diameter])
            final_count = max(count_by_grid, min_needed)

            # ===== 8. Корректируем для больших домов =====
            area = length * width
            if area > 100 and final_count > 20:
                final_count = 20
                print(f"📐 Площадь >100м², ограничиваем до 20 свай")
                # Пересчитываем диаметр
                required_capacity = total_weight / final_count
                for diam in sorted_diameters:
                    capacity = pile_capacity[diam]
                    if capacity >= required_capacity:
                        selected_diameter = diam
                        break

            # ===== 9. ФИНАЛЬНЫЙ ВЫВОД =====
            print(f"🎯 ИТОГ: диаметр {selected_diameter}мм, {final_count} свай")
            print(f"   (по сетке: {count_by_grid}, нужно по нагрузке: {min_needed})")
            print(f"   Вес дома: {total_weight} т")

            return {
                'diameter': selected_diameter,
                'count': final_count,
                'grid_count': count_by_grid,
                'min_needed': min_needed,
                'total_weight': total_weight,
                'cols': cols,  # добавим для отображения
                'rows': rows  # добавим для отображения
            }

        except Exception as e:
            print(f"❌ Ошибка в get_pile_result: {e}")
            return None

    def get_blade_width(self, diameter):
        """Возвращает ширину лопасти по диаметру сваи"""
        blade_widths = {
            57: 200,
            76: 200,
            89: 250,
            108: 300,
            133: 350
        }
        return blade_widths.get(diameter, 250)

    def set_spreadsheet(self, spreadsheet_url_or_id):
        """Устанавливает таблицу для работы"""
        try:
            self.spreadsheet = self.client.open_by_url(spreadsheet_url_or_id)
            print(f"✅ Открыта таблица: {self.spreadsheet.title}")

            # Инициализируем листы (названия как у тебя)
            self.price_sheet = self.spreadsheet.worksheet("Стоимость винтовых свай")
            print(f"✅ Подключен лист: Стоимость винтовых свай")

            self.delivery_sheet = self.spreadsheet.worksheet("Стоимость доставки")
            print(f"✅ Подключен лист: Стоимость доставки")

            self.mounting_sheet = self.spreadsheet.worksheet("Возможность монтажа")
            print(f"✅ Подключен лист: Возможность монтажа")

            self.time_sheet = self.spreadsheet.worksheet("Время бурояма")
            print(f"✅ Подключен лист: Время бурояма")

            self.schedule_sheet = self.spreadsheet.worksheet("График монтажей")
            print(f"✅ Подключен лист: График монтажей")

            logging.info(f"✅ Connected to spreadsheet")
            return True
        except Exception as e:
            logging.error(f"❌ Failed to connect to spreadsheet: {e}")
            print(f"❌ ОШИБКА подключения к таблице: {e}")
            return False

    # ========== НОВЫЕ ФУНКЦИИ ДЛЯ МЕДИА ==========

    def save_file_id(self, url, file_id):
        """Сохраняет file_id в отдельный лист"""
        try:
            # Получаем или создаем лист "Медиа"
            try:
                media_sheet = self.spreadsheet.worksheet("Медиа")
            except:
                media_sheet = self.spreadsheet.add_worksheet("Медиа", 1000, 3)
                media_sheet.append_row(["URL", "FILE_ID", "ДАТА"])

            media_sheet.append_row([
                url,
                file_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ])
            print(f"✅ File ID сохранен для {url}")
        except Exception as e:
            print(f"❌ Ошибка сохранения file_id: {e}")

    def get_file_id(self, url):
        """Ищет file_id по URL"""
        try:
            media_sheet = self.spreadsheet.worksheet("Медиа")
            all_rows = media_sheet.get_all_values()

            for row in all_rows[1:]:  # пропускаем заголовок
                if len(row) >= 2 and row[0] == url:
                    return row[1]  # file_id
            return None
        except Exception as e:
            print(f"❌ Ошибка получения file_id: {e}")
            return None

    # ========== КОНЕЦ НОВЫХ ФУНКЦИЙ ==========

    def parse_pile_type(self, pile_name):
        """Парсит название сваи на диаметр и длину"""
        try:
            # Пример: "Свая винтовая СВСН-108/2500/300"
            parts = pile_name.split('-')[1].split('/')
            diameter = int(parts[0])
            length = int(parts[1])
            return diameter, length
        except:
            return None, None

    def get_pile_price(self, diameter, length):
        """Получает цену сваи по диаметру и длине"""
        if not self.price_sheet:
            return None

        try:
            # Ищем по колонкам: A - название, B - цена
            all_rows = self.price_sheet.get_all_values()

            # Пропускаем заголовок
            for row in all_rows[1:]:
                if len(row) < 2:
                    continue

                name = row[0]
                if f"{diameter}" in name and f"{length}" in name:
                    # Убираем пробелы из цены (пример: "1 600" -> "1600")
                    price_str = row[1].replace(' ', '').replace(' ', '')
                    try:
                        return int(price_str)
                    except:
                        return None
            return None
        except Exception as e:
            logging.error(f"Error getting pile price: {e}")
            return None

    def get_head_price(self, diameter):
        """Получает цену оголовка по диаметру сваи"""
        if not self.price_sheet:
            return None

        try:
            all_rows = self.price_sheet.get_all_values()

            # Ищем в конце файла, где оголовки
            for row in all_rows:
                if len(row) < 2:
                    continue

                name = row[0]
                if "Оголовок" in name and str(diameter) in name:
                    price_str = row[1].replace(' ', '').replace(' ', '')
                    try:
                        return int(price_str)
                    except:
                        return None
            return 300  # дефолт если не нашли
        except Exception as e:
            logging.error(f"Error getting head price: {e}")
            return 300

    def get_district_info(self, district):
        """Получает информацию о районе: доставка, возможность монтажа, время"""
        if not self.mounting_sheet or not self.delivery_sheet or not self.time_sheet:
            return None

        try:
            # 1. Проверяем возможность монтажа
            mounting_rows = self.mounting_sheet.get_all_values()
            mounting_info = None

            for row in mounting_rows[1:]:  # пропускаем заголовок
                if len(row) >= 4 and row[0].strip().lower() == district.strip().lower():
                    mounting_info = {
                        'district': row[0],
                        'small_available': row[1].lower() == 'да',
                        'big_available': row[2].lower() == 'да',
                        'manual_available': row[3].lower() == 'да'  # ← колонка D (индекс 3)
                    }
                    break

            if not mounting_info:
                return None

            # 2. Получаем стоимость доставки
            delivery_rows = self.delivery_sheet.get_all_values()
            delivery_prices = None

            for row in delivery_rows[1:]:
                if len(row) >= 6 and row[0].strip().lower() == district.strip().lower():
                    delivery_prices = {
                        'gazelle': int(row[1].replace(' ', '').replace(' ', '')),
                        'board_4m_1_5t': int(row[2].replace(' ', '').replace(' ', '')),
                        'board_4m_3t': int(row[3].replace(' ', '').replace(' ', '')),
                        'board_6m_5t': int(row[4].replace(' ', '').replace(' ', '')),
                        'self_loader_5t': int(row[5].replace(' ', '').replace(' ', ''))
                    }
                    break

            # 3. Получаем время бурояма
            time_rows = self.time_sheet.get_all_values()
            time_info = None

            for row in time_rows[1:]:
                if len(row) >= 6 and row[0].strip().lower() == district.strip().lower():
                    time_info = {
                        'small_20': int(row[1]) if row[1] else 4,
                        'small_30': int(row[2]) if row[2] else 5,
                        'small_35': int(row[3]) if row[3] else 6,
                        'big_35': int(row[4]) if row[4] else 4,
                        'big_45': int(row[5]) if row[5] else 5,
                        'price_per_hour': 3500,  # из таблицы
                        'manual_price_per_pile': 2500,
                        'machine_price_per_pile': 1700
                    }
                    break

            return {
                'mounting': mounting_info,
                'delivery': delivery_prices,
                'time': time_info
            }

        except Exception as e:
            logging.error(f"Error getting district info: {e}")
            return None

    def calculate_mounting_time(self, pile_count, equipment, district_info):
        """Рассчитывает время монтажа в зависимости от количества свай"""
        print(f"🔥 DEBUG calculate_mounting_time: pile_count={pile_count}, equipment={equipment}")
        print(f"🔥 district_info={district_info}")

        if not district_info:
            print("🔥 district_info is None!")
            return 4

        if not district_info.get('time'):
            print("🔥 district_info['time'] is None!")
            return 4

        time_data = district_info['time']
        print(f"🔥 time_data={time_data}")

        if equipment == 'small':
            if pile_count <= 20:
                return time_data.get('small_20', 4)
            elif pile_count <= 30:
                return time_data.get('small_30', 5)
            else:
                return time_data.get('small_35', 6)
        elif equipment == 'big':
            if pile_count <= 35:
                return time_data.get('big_35', 4)
            else:
                return time_data.get('big_45', 5)
        else:
            return 0

    def save_tnps_score(self, order_number, score):
        """Сохраняет оценку TNPS в последнюю колонку таблицы"""
        if not self.schedule_sheet:
            return False

        try:
            all_rows = self.schedule_sheet.get_all_values()

            for i, row in enumerate(all_rows, start=1):
                if len(row) >= 3 and row[2] == str(order_number):
                    # Предполагаем, что последняя колонка (статус) сейчас в колонке H
                    # Значит следующая свободная колонка - I (индекс 9)
                    self.schedule_sheet.update_cell(i, 10, score)
                    print(f"✅ Оценка {score} для заказа #{order_number} сохранена")
                    return True

            return False
        except Exception as e:
            print(f"❌ Ошибка сохранения оценки: {e}")
            return False

    def get_all_districts(self):
        """Возвращает список всех районов из таблицы без спама в консоль"""
        if not self.mounting_sheet:
            logging.error("❌ mounting_sheet не инициализирован!")
            return []

        try:
            all_rows = self.mounting_sheet.get_all_values()

            districts = []
            for row in all_rows[1:]:  # пропускаем заголовок
                if row and len(row) > 0 and row[0].strip():
                    districts.append(row[0].strip())

            # Печатаем только результат один раз
            print(f"✅ Успешно загружено районов: {len(districts)}")
            return districts
        except Exception as e:
            logging.error(f"Error getting districts: {e}")
            print(f"❌ ОШИБКА в get_all_districts: {e}")
            return []

    def get_free_slots(self, date):
        """Возвращает список свободного времени на указанную дату"""
        if not self.schedule_sheet:
            return []

        try:
            from datetime import datetime, timedelta

            all_rows = self.schedule_sheet.get_all_values()
            taken_times = []

            # Пропускаем заголовок
            for row in all_rows[1:]:
                if len(row) >= 2 and row[0] == date:
                    taken_times.append(row[1])  # время

            # Все возможные слоты с 9 до 20
            all_slots = [f"{h:02d}:00" for h in range(9, 21)]

            # Проверяем, сегодня ли выбранная дата
            today = datetime.now().strftime("%d.%m.%Y")
            current_hour = datetime.now().hour

            available_slots = []
            for slot in all_slots:
                slot_hour = int(slot.split(':')[0])

                # Если дата сегодняшняя - проверяем, что время не прошло
                if date == today:
                    if slot_hour <= current_hour:
                        continue  # пропускаем прошедшее время
                    if slot_hour == current_hour:
                        # Можно добавить запас в 1 час
                        if datetime.now().minute > 30:
                            continue

                # Проверяем, не занято ли время
                if slot not in taken_times:
                    available_slots.append(slot)

            return available_slots
        except Exception as e:
            print(f"❌ Ошибка при получении свободных слотов: {e}")
            return []

    def update_order_status(self, order_number, status):
        """Обновляет статус заказа в графике монтажа"""
        if not self.schedule_sheet:
            return False

        try:
            all_rows = self.schedule_sheet.get_all_values()

            for i, row in enumerate(all_rows, start=1):
                if len(row) >= 3 and row[2] == str(order_number):
                    # Статус в колонке H (индекс 8)
                    self.schedule_sheet.update_cell(i, 8, status)
                    print(f"✅ Статус заказа #{order_number} обновлен на '{status}'")
                    return True

            return False
        except Exception as e:
            print(f"❌ Error updating status: {e}")
            return False

    def get_order_address(self, order_number):
        """Получает адрес доставки заказа по номеру"""
        if not self.schedule_sheet:
            return None

        try:
            all_rows = self.schedule_sheet.get_all_values()

            for row in all_rows[1:]:  # пропускаем заголовок
                if len(row) >= 3 and row[2] == str(order_number):
                    # Адрес в колонке E (индекс 4)
                    return row[4] if len(row) > 4 else None

            return None
        except Exception as e:
            print(f"❌ Error getting order address: {e}")
            return None

    def update_order_driver(self, order_number, driver_name):
        """Обновляет водителя в графике монтажа по номеру заказа"""
        if not self.schedule_sheet:
            return False

        try:
            all_rows = self.schedule_sheet.get_all_values()

            for i, row in enumerate(all_rows, start=1):
                if len(row) >= 3 and row[2] == str(order_number):
                    # Водитель в колонке G (индекс 7)
                    self.schedule_sheet.update_cell(i, 7, driver_name)
                    print(f"✅ Водитель для заказа #{order_number} обновлен на '{driver_name}'")
                    return True

            return False
        except Exception as e:
            print(f"❌ Ошибка обновления водителя: {e}")
            return False

    def add_order_to_schedule(self, date, time, order_number, client, address, brigade="", driver="",
                              status="запланирован", chat_id="", service_type="", delivery_type=""):
        """Добавляет заказ в график монтажа"""
        if not self.schedule_sheet:
            return False

        try:
            # Добавляем строку с колонками:
            # A-дата, B-время, C-номер, D-клиент, E-адрес, F-бригада, G-водитель, H-статус, I-chat_id, J-оценка, K-тип услуги, L-тип доставки
            row = [
                date,  # A
                time,  # B
                str(order_number),  # C
                client,  # D
                address,  # E
                brigade,  # F
                driver,  # G
                status,  # H
                str(chat_id),  # I
                "",  # J (оценка, пока пусто)
                service_type,  # K (тип услуги)
                delivery_type  # L (тип доставки)
            ]
            self.schedule_sheet.append_row(row)
            print(f"✅ Заказ #{order_number} добавлен в график: service={service_type}, delivery={delivery_type}")
            return True
        except Exception as e:
            print(f"❌ Error adding order to schedule: {e}")
            return False

    def get_client_chat_id(self, order_number):
        """Получает chat_id клиента по номеру заказа"""
        if not self.schedule_sheet:
            return None

        try:
            all_rows = self.schedule_sheet.get_all_values()

            for row in all_rows[1:]:  # пропускаем заголовок
                if len(row) >= 3 and row[2] == str(order_number):
                    # Chat ID в колонке I (индекс 8)
                    return int(row[8]) if len(row) > 8 and row[8] else None

            return None
        except Exception as e:
            print(f"❌ Error getting client chat_id: {e}")
            return None

    def update_order_brigade(self, order_number, brigade_name):
        """Обновляет бригаду в графике монтажа по номеру заказа"""
        if not self.schedule_sheet:
            return False

        try:
            # Ищем строку с заказом
            all_rows = self.schedule_sheet.get_all_values()
            print(f"🔍 Ищем заказ #{order_number}")

            for i, row in enumerate(all_rows, start=1):
                print(f"Строка {i}: {row}")
                if len(row) >= 3 and row[2] == str(order_number):
                    print(f"✅ Нашли! Строка {i}, текущая бригада: {row[5] if len(row) > 5 else 'нет'}")
                    # Обновляем бригаду (колонка F - индекс 6)
                    self.schedule_sheet.update_cell(i, 6, brigade_name)
                    print(f"✅ Обновлено на {brigade_name}")
                    return True

            print(f"❌ Заказ #{order_number} не найден")
            return False
        except Exception as e:
            print(f"❌ Error updating brigade: {e}")
            return False


# Создаём глобальный экземпляр
sheets = GoogleSheets()