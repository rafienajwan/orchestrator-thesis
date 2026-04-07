FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "fastapi>=0.115.0,<1.0.0" "uvicorn[standard]>=0.34.0,<1.0.0" "pydantic>=2.10.0,<3.0.0"

COPY main.py ./main.py

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
