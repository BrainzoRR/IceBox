# config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = "IceBoxTbot"
YOOKASSA_SHOP_ID = os.getenv("1245114")
YOOKASSA_SECRET_KEY = os.getenv("live_c9qqTa2V87xWHomMI-i2m6XQZI0_eu3J7Dyz7aVZAx8")
DB_PATH = "icebox.db"
FREE_LIMIT = 50