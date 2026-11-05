FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Провайдер только читает файлы и ходит в сеть — права root ему не нужны.
RUN useradd --system --no-create-home --uid 10001 provider
USER provider

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
