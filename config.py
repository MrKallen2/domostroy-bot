import os
from dotenv import load_dotenv

load_dotenv()

# Основные настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# ID группы и топики
GROUP_ID = int(os.getenv("GROUP_ID", "-1003753687080"))
THREAD_DOMOSTROY = int(os.getenv("THREAD_DOMOSTROY", "2"))
THREAD_MONTAGNIKI = int(os.getenv("THREAD_MONTAGNIKI", "3"))
THREAD_POSTAVSHCHIKI = int(os.getenv("THREAD_POSTAVSHCHIKI", "4"))
THREAD_DOSTAVKA = int(os.getenv("THREAD_DOSTAVKA", "5"))

# Google Sheets
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google-key.json")

# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# Склад
WAREHOUSE_ADDRESS = os.getenv("WAREHOUSE_ADDRESS", "г. Новосибирск, ул. Складская, д. 10, стр. 5")

# URLs таблиц (оставляем как было)
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1SPRVDmUUP_NFXo_fb_4g5XFaFaqasXEKZPcY-s8uYBQ/edit?gid=1480051609#gid=1480051609"
CALC_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1eiXC5N-EbIltkb1eOF5sDaxdxAG15QEUpscDfXKXxTE/edit?usp=sharing"

# Проверка обязательных переменных
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле!")
if not YOOKASSA_SECRET_KEY:
    print("⚠️ YOOKASSA_SECRET_KEY не найден — платежи работать не будут")