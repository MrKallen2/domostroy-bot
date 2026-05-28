import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

logger = logging.getLogger(__name__)

# Настройки почты (ИСПРАВЬ НА СВОИ)
SMTP_SERVER = "smtp.gmail.com"  # Для Gmail
SMTP_PORT = 587
SMTP_EMAIL = "mskroleplay8@gmail.com"  # ← ТВОЯ ПОЧТА
SMTP_PASSWORD = "Lunatik1982"  # ← ПАРОЛЬ ПРИЛОЖЕНИЯ


async def send_order_to_email(order_data: dict) -> bool:
    """Отправляет данные заказа на почту через SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_EMAIL
        msg['To'] = SMTP_EMAIL  # Кому отправляем (можно другой email)
        msg['Subject'] = f"Новый заказ #{order_data.get('order_number')}"

        # Формируем тело письма
        body = f"""
НОВЫЙ ЗАКАЗ #{order_data.get('order_number')}

КЛИЕНТ:
ФИО: {order_data.get('fio')}
Телефон: {order_data.get('phone')}
Адрес: {order_data.get('address')}
Район: {order_data.get('district')}

ЗАКАЗ:
Дом: {order_data.get('length')}×{order_data.get('width')}м
Материал: {order_data.get('material')}
Этажей: {order_data.get('floors')}
Сваи: {order_data.get('pile_count')} шт ({order_data.get('pile_type')})
Предоплата: {order_data.get('prepayment')} ₽
"""

        if order_data.get('selected_date'):
            body += f"\nДата: {order_data.get('selected_date')} {order_data.get('selected_time')}"

        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # Отправляем
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        logger.info(f"✅ Заказ #{order_data.get('order_number')} отправлен на почту")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")
        return False