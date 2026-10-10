FROM python:3.13-slim
WORKDIR /app
COPY * ./
RUN pip install requirements.txt
EXPOSE 8000
CMD []
