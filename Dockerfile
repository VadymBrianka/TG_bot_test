FROM python:3.11-slim

WORKDIR /app

# Копіюємо та встановлюємо залежності
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо код бота
COPY . .

# Запускаємо бота
CMD ["python", "bot.py"]