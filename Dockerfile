FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY truenas_exporter.py .

USER 65534:65534
EXPOSE 9814
ENTRYPOINT ["python", "-u", "/app/truenas_exporter.py"]
