FROM python:3.12-alpine
RUN pip install --no-cache-dir 'PyJWT[crypto]>=2.10,<3'
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["python","-u","server.py"]