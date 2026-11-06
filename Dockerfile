FROM python:3.12-alpine
RUN apk add --no-cache curl ca-certificates poppler-utils
RUN pip install --no-cache-dir 'PyJWT[crypto]>=2.10,<3' 'Pillow>=11,<13'
WORKDIR /app
COPY app/ /app/
COPY tests/ /app/tests/
EXPOSE 8080
CMD ["python","-u","server.py"]