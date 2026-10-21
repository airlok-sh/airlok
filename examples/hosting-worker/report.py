# Задача по расписанию: запускается отдельно от воркера, с теми же переменными и базой.
import os
from datetime import datetime

print(f"отчёт собран в {datetime.now():%H:%M:%S}, переменных окружения: {len(os.environ)}")
