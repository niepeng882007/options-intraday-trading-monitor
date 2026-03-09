FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

CMD ["python", "-m", "src.main"]
