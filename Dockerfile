FROM python:3.12-alpine
RUN pip install --no-cache-dir Pillow==11.3.0
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["python","-u","server.py"]