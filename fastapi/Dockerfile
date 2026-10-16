FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 8000
CMD ["fastapi", "run", "mbta_weather_api.py", "--host", "0.0.0.0", "--port", "8000"]