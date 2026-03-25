FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -r requirements.txt

COPY *.py ./

# Futu SDK writes logs to $HOME/.com.futunn.FutuOpenD — ensure writable
RUN mkdir -p /app/.home && chmod 777 /app/.home
ENV HOME=/app/.home
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
