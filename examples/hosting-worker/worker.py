# Воркер: каждые 10 секунд пишет в лог. Так же работает бот на long polling.
import time
from datetime import datetime

n = 0
while True:
    n += 1
    print(f"тик {n}: {datetime.now():%H:%M:%S}", flush=True)
    time.sleep(10)
