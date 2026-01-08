# sheets.py
import asyncio
import gspread_asyncio
from google.oauth2.service_account import Credentials
from datetime import datetime

# 1. Функция авторизации (читает creds.json)
def get_creds():
    creds = Credentials.from_service_account_file("creds.json")
    scoped = creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ])
    return scoped

class GoogleSheetsManager:
    def __init__(self, sheet_url):
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)
        self.sheet_url = sheet_url

    # Внутренний метод для получения доступа к листу
    async def _get_worksheet(self, name):
        agc = await self.agcm.authorize()
        ss = await agc.open_by_url(self.sheet_url)
        return await ss.worksheet(name)

    # --- МЕТОД 1: ЗАПИСЬ НОВОГО КЛИЕНТА ---
    async def add_user(self, user_id, username, name, phone, lat, lon):
        try:
            ws = await self._get_worksheet("Users") # Имя вкладки в таблице
            row = [
                str(user_id),
                f"@{username}" if username else "No Username",
                str(name),
                str(phone),
                str(lat),
                str(lon),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ]
            await ws.append_row(row)
            print(f"[Sheets] User {user_id} saved.")
        except Exception as e:
            print(f"[Sheets Error] Add user failed: {e}")

    # --- МЕТОД 2: ЗАПИСЬ НОВОГО РЕСТОРАНА ---
    async def add_restaurant(self, rest_name, lat, lon):
        try:
            ws = await self._get_worksheet("Restaurants") # Имя вкладки
            row = [
                "Auto-ID", 
                str(rest_name),
                str(lat),
                str(lon),
                "0", # Стартовое кол-во боксов
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ]
            await ws.append_row(row)
            print(f"[Sheets] Restaurant {rest_name} saved.")
        except Exception as e:
            print(f"[Sheets Error] Add restaurant failed: {e}")
