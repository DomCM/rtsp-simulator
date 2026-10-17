FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app
COPY publisher.py .

ENTRYPOINT ["python", "-u", "publisher.py"]
