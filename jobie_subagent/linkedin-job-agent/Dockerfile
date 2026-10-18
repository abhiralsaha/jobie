FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.py .
RUN useradd -m bot && mkdir -p /app/data && chown -R bot /app
USER bot
VOLUME /app/data
EXPOSE 8080
CMD ["python", "run.py"]
