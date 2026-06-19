FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8077

CMD ["uvicorn", "server:app", \
     "--host", "0.0.0.0", \
     "--port", "8077", \
     "--root-path", "/toolkit-assistant", \
     "--proxy-headers", \
     "--forwarded-allow-ips=*"]
