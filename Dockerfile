FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/

RUN mkdir -p cache

EXPOSE 8000

# Use host network so SSDP multicast discovery works
# and DLNA devices can reach the /stream endpoint
CMD ["python", "app.py"]
