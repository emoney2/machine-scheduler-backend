import time
from threading import Lock
from sheets import SHEETS

class SheetCache:
    def __init__(self, ttl=15):
        self.cache = {}
        self.ttl = ttl
        self.lock = Lock()

    def get_sheet_data(self, spreadsheet_id, sheet_name):
        key = (spreadsheet_id, sheet_name)
        now = time.time()

        with self.lock:
            if key in self.cache:
                entry = self.cache[key]
                if now - entry['timestamp'] < self.ttl:
                    return entry['data']

            sheet = SHEETS.get_worksheet(sheet_name)
            data = sheet.get_all_values()
            self.cache[key] = {'data': data, 'timestamp': now}
            return data

# ✅ Exported singleton instance
sheet_cache = SheetCache()