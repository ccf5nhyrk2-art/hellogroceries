FROM python:3.12-alpine
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["python","-u","server.py"]